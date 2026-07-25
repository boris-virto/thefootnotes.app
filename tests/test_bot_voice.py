"""Интеграционные тесты голосового: сбой облака, фолбэк на локальный Whisper."""
from types import SimpleNamespace

import pytest

from app import bot, db, errors, llm


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


async def _get_file(_file_id):
    async def _download():
        return bytearray(b"fake-audio")
    return SimpleNamespace(download_as_bytearray=_download)


def _voice_update():
    message = SimpleNamespace(
        chat=SimpleNamespace(send_action=Recorder()),
        voice=SimpleNamespace(file_id="f1"),
        reply_text=Recorder(),
    )
    user = SimpleNamespace(id=111, username="u", first_name="U")
    update = SimpleNamespace(
        effective_user=user, effective_message=message, message=message,
        effective_chat=SimpleNamespace(id=111),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(get_file=_get_file, send_message=Recorder())
    )
    return update, context, message, context.bot.send_message


def _fail(*_a, **_k):
    raise RuntimeError("whisper 500")


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setattr(errors.config, "ADMIN_TELEGRAM_ID", 999)


async def test_local_fallback_saves_note_and_warns_user(monkeypatch):
    monkeypatch.setattr("app.transcribe.transcribe_remote", _fail)
    monkeypatch.setattr("app.transcribe.transcribe_local", lambda _a: "привет мир")
    monkeypatch.setattr("app.llm.structure_text", lambda _t: llm.ExtractedReminder(title="привет мир"))

    update, context, message, admin_send = _voice_update()
    await bot.handle_voice(update, context)

    texts = [c[0][0] for c in message.reply_text.calls]
    # пользователя предупредили о локальном (более долгом) распознавании
    assert any("локально" in t.lower() for t in texts)
    # заметка сохранилась
    assert len(db.list_board()) == 1
    assert db.list_board()[0].title == "привет мир"
    # фолбэк сработал успешно — админа не дёргаем
    assert admin_send.calls == []


async def test_both_fail_notifies_admin_and_saves_nothing(monkeypatch):
    monkeypatch.setattr("app.transcribe.transcribe_remote", _fail)

    def _local_fail(_a):
        raise RuntimeError("local boom")

    monkeypatch.setattr("app.transcribe.transcribe_local", _local_fail)

    update, context, message, admin_send = _voice_update()
    await bot.handle_voice(update, context)

    texts = [c[0][0] for c in message.reply_text.calls]
    assert any("ни в облаке, ни локально" in t.lower() for t in texts)
    # админа уведомили с трейсбеком локального сбоя
    assert admin_send.calls and "local boom" in admin_send.calls[-1][0][1]
    # ничего не сохранилось
    assert db.list_board() == []
