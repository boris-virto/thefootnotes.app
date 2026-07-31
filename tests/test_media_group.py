"""Альбом из Telegram = одно мероприятие с несколькими файлами, а не N карточек."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import bot, db, errors, llm


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class FakeJob:
    def __init__(self, callback, data, name):
        self.callback, self.data, self.name = callback, data, name
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    """Имитирует дебаунс PTB: помним запланированные задачи и умеем их «запустить»."""

    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when, data=None, name=None):
        job = FakeJob(callback, data, name)
        self.jobs.append(job)
        return job

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]

    async def fire_pending(self, context):
        """Выполняет живые задачи — как будто пауза после последнего файла истекла."""
        for job in [j for j in self.jobs if not j.removed]:
            context.job = job
            await job.callback(context)


def _album_message(group_id, caption=None):
    return SimpleNamespace(
        media_group_id=group_id,
        caption=caption,
        chat=SimpleNamespace(id=111, send_action=Recorder()),
        from_user=SimpleNamespace(id=111, username="u", first_name="U"),
        document=SimpleNamespace(file_id="fid", file_unique_id="uid", mime_type="application/pdf"),
        reply_text=Recorder(),
    )


def _ctx(job_queue):
    return SimpleNamespace(
        bot=SimpleNamespace(get_file=None, send_message=Recorder()),
        job_queue=job_queue,
        application=SimpleNamespace(job_queue=job_queue),
        job=None,
    )


@pytest.fixture(autouse=True)
def _wiring(monkeypatch, tmp_path):
    monkeypatch.setattr(errors.config, "ADMIN_TELEGRAM_ID", 999)
    monkeypatch.setattr(bot, "FILES_DIR", tmp_path)
    # Скачивание подменяем: пишем файл на диск и возвращаем «содержимое».

    async def _fake_download(context, file_id, path):
        Path(path).write_bytes(b"%PDF-fake")
        return b"%PDF-fake"

    monkeypatch.setattr(bot, "_download", _fake_download)
    bot._media_groups.clear()
    yield
    bot._media_groups.clear()


def _one_ticket(_files):
    return llm.ExtractedReminder(title="LJUBAV FESTIVAL", event_date="2026-08-22",
                                category="ticket", notes="2 билета")


async def test_album_creates_single_card_with_all_files(monkeypatch):
    """Регрессия: два билета одним сообщением давали две карточки."""
    seen = {}

    def _structure(files):
        seen["count"] = len(files)   # модель должна увидеть оба файла сразу
        return _one_ticket(files)

    monkeypatch.setattr("app.llm.structure_files", _structure)

    jq = FakeJobQueue()
    context = _ctx(jq)
    for unique in ("t1", "t2"):
        message = _album_message("group-1")
        message.document.file_unique_id = unique
        update = SimpleNamespace(
            message=message, effective_message=message,
            effective_user=message.from_user, effective_chat=message.chat,
        )
        await bot.handle_document(update, context)

    # Пока пауза не истекла — ни одной карточки, и запланирована ровно одна задача.
    assert db.list_board() == []
    assert len(jq.get_jobs_by_name("mg:group-1")) == 1

    await jq.fire_pending(context)

    board = db.list_board()
    assert len(board) == 1, "альбом должен дать ровно одну карточку"
    assert board[0].title == "LJUBAV FESTIVAL"
    assert len(board[0].file_paths) == 2, "оба билета должны быть приложены"
    assert seen["count"] == 2, "оба файла должны уйти в модель одним запросом"


async def test_single_document_still_creates_one_card(monkeypatch):
    monkeypatch.setattr("app.llm.structure_files",
                        lambda files: llm.ExtractedReminder(title="Один билет"))
    jq = FakeJobQueue()
    context = _ctx(jq)
    message = _album_message(None)  # обычное сообщение, не альбом
    update = SimpleNamespace(
        message=message, effective_message=message,
        effective_user=message.from_user, effective_chat=message.chat,
    )
    await bot.handle_document(update, context)

    board = db.list_board()
    assert len(board) == 1 and len(board[0].file_paths) == 1
    assert jq.jobs == []  # дебаунс для одиночного файла не нужен


async def test_second_file_reschedules_debounce(monkeypatch):
    """Каждый новый файл альбома сдвигает разбор, чтобы дождаться остальных."""
    monkeypatch.setattr("app.llm.structure_files", _one_ticket)
    jq = FakeJobQueue()
    context = _ctx(jq)
    for unique in ("a", "b", "c"):
        message = _album_message("group-2")
        message.document.file_unique_id = unique
        update = SimpleNamespace(
            message=message, effective_message=message,
            effective_user=message.from_user, effective_chat=message.chat,
        )
        await bot.handle_document(update, context)

    assert len(jq.jobs) == 3                          # планировали трижды…
    assert len(jq.get_jobs_by_name("mg:group-2")) == 1  # …но живая только последняя
    await jq.fire_pending(context)
    assert len(db.list_board()) == 1


async def test_two_albums_do_not_mix(monkeypatch):
    monkeypatch.setattr(
        "app.llm.structure_files",
        lambda files: llm.ExtractedReminder(title=f"Событие из {len(files)}"),
    )
    jq = FakeJobQueue()
    context = _ctx(jq)
    for group, uniques in (("g1", ("x1", "x2")), ("g2", ("y1",))):
        for unique in uniques:
            message = _album_message(group)
            message.document.file_unique_id = unique
            update = SimpleNamespace(
                message=message, effective_message=message,
                effective_user=message.from_user, effective_chat=message.chat,
            )
            await bot.handle_document(update, context)
    await jq.fire_pending(context)

    board = db.list_board()
    assert len(board) == 2
    assert {len(r.file_paths) for r in board} == {2, 1}


def test_add_reminder_with_several_files():
    r = db.add_reminder(title="Два билета", file_paths=["/a.pdf", "/b.pdf"])
    assert r.file_paths == ["/a.pdf", "/b.pdf"]
    assert r.file_path == "/a.pdf"  # legacy-поле синхронно с первым файлом
    assert db.get_reminder(r.id).file_paths == ["/a.pdf", "/b.pdf"]


def test_add_attachments_appends_to_existing_card():
    r = db.add_reminder(title="Один билет", file_paths=["/a.pdf"])
    db.add_attachments(r.id, ["/b.pdf", "/c.pdf"])
    assert db.get_reminder(r.id).file_paths == ["/a.pdf", "/b.pdf", "/c.pdf"]


def test_legacy_card_exposes_its_single_file():
    """У карточек до появления вложений file_paths берётся из file_path."""
    from sqlalchemy import text

    with db.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO reminders (title, category, status, importance, done, source, "
            "file_path, remind_active, created_at) VALUES ('старая','ticket','todo',2,0,"
            "'pdf','/old/ticket.pdf',1,'2026-01-01 00:00:00')"
        ))
    old = [r for r in db.list_board() if r.title == "старая"][0]
    assert old.file_paths == ["/old/ticket.pdf"]
    db._backfill_attachments()
    refreshed = db.get_reminder(old.id)
    assert [a.path for a in refreshed.attachments] == ["/old/ticket.pdf"]
    # повторный запуск не должен продублировать вложение
    db._backfill_attachments()
    assert len(db.get_reminder(old.id).attachments) == 1


async def test_album_failure_notifies_and_saves_nothing(monkeypatch):
    def _boom(_files):
        raise RuntimeError("claude down")

    monkeypatch.setattr("app.llm.structure_files", _boom)
    jq = FakeJobQueue()
    context = _ctx(jq)
    message = _album_message("group-3")
    update = SimpleNamespace(
        message=message, effective_message=message,
        effective_user=message.from_user, effective_chat=message.chat,
    )
    await bot.handle_document(update, context)
    await jq.fire_pending(context)

    assert db.list_board() == []
    texts = [c[0][0] for c in message.reply_text.calls]
    assert any("не смог разобрать" in t.lower() for t in texts)
    # админ получил трейсбек
    assert context.bot.send_message.calls
    assert "claude down" in context.bot.send_message.calls[-1][0][1]
