"""Слой захвата — общий для бота и мобильного клиента, поэтому проверяем его отдельно
от Telegram: те же правила должны работать для карточки из чата и из приложения."""
import pytest

from app import db, ingest, llm


@pytest.fixture
def fake_llm(monkeypatch):
    """Подменяем модель: тесты про правила захвата, а не про качество разбора."""
    seen = {}

    def structure_text(text):
        seen["text"] = text
        return llm.ExtractedReminder(title="Разобрано", category="task")

    def structure_files(files):
        seen["files"] = files
        return llm.ExtractedReminder(title="Билет", category="ticket", event_date="2026-09-01")

    def structure_pdf_url(url):
        seen["pdf_url"] = url
        return llm.ExtractedReminder(title="PDF по ссылке", category="ticket")

    monkeypatch.setattr("app.llm.structure_text", structure_text)
    monkeypatch.setattr("app.llm.structure_files", structure_files)
    monkeypatch.setattr("app.llm.structure_pdf_url", structure_pdf_url)
    return seen


async def test_text_creates_card_and_keeps_link(fake_llm):
    result = await ingest.ingest_text("сходить на выставку https://ex.com/afisha", chat_id=7)
    assert result.reminder.title == "Разобрано"
    assert result.reminder.source == "text"
    assert result.reminder.chat_id == 7
    # Ссылку достаём кодом из исходного текста, а не через модель.
    assert result.reminder.url == "https://ex.com/afisha"


async def test_pdf_link_goes_to_document_branch(fake_llm):
    result = await ingest.ingest_text("вот билет https://ex.com/t.pdf", chat_id=1)
    assert fake_llm["pdf_url"] == "https://ex.com/t.pdf"
    assert result.reminder.source == "pdf"
    assert "text" not in fake_llm, "PDF-ссылку не надо разбирать как обычный текст"


async def test_bare_non_pdf_link_is_rejected(fake_llm):
    """Сообщение из одной ссылки на веб-страницу — мусорная карточка, не сохраняем."""
    with pytest.raises(ingest.Rejected):
        await ingest.ingest_text("https://ex.com/page", chat_id=1)
    assert db.list_board() == []


async def test_empty_text_is_rejected(fake_llm):
    with pytest.raises(ingest.Rejected):
        await ingest.ingest_text("   ", chat_id=1)


async def test_llm_failure_becomes_failed_with_traceback(monkeypatch):
    def boom(_text):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.llm.structure_text", boom)
    with pytest.raises(ingest.Failed) as exc:
        await ingest.ingest_text("купить хлеб", chat_id=1)
    assert exc.value.where == "text"
    # Исходное исключение должно дожить до отчёта об ошибке — иначе трейсбек потерян.
    assert isinstance(exc.value.original, RuntimeError)
    assert db.list_board() == []


async def test_several_files_make_one_card(fake_llm):
    result = await ingest.ingest_files(
        [
            ingest.IngestFile(b"\x89PNG-1", "image/png"),
            ingest.IngestFile(b"\x89PNG-2", "image/png"),
        ],
        source="photo",
        chat_id=1,
    )
    assert len(db.list_board()) == 1, "альбом = одно мероприятие"
    assert len(result.reminder.file_paths) == 2
    assert len(fake_llm["files"]) == 2, "оба файла должны уйти в модель одним запросом"


async def test_files_without_path_are_stored_on_disk(fake_llm):
    result = await ingest.ingest_files(
        [ingest.IngestFile(b"%PDF-fake", "application/pdf")], source="pdf", chat_id=1
    )
    saved = ingest.file_for(result.reminder, 0)
    assert saved is not None and saved.read_bytes() == b"%PDF-fake"
    assert ingest.file_for(result.reminder, 1) is None  # второго файла нет


async def test_same_file_twice_reuses_one_path(fake_llm):
    """Имя файла — от содержимого: один и тот же билет не занимает место дважды."""
    first = await ingest.ingest_files(
        [ingest.IngestFile(b"same-bytes", "image/jpeg")], source="photo", chat_id=1
    )
    second = await ingest.ingest_files(
        [ingest.IngestFile(b"same-bytes", "image/jpeg")], source="photo", chat_id=1
    )
    assert first.reminder.file_paths == second.reminder.file_paths


async def test_unsupported_media_type_is_rejected(fake_llm):
    with pytest.raises(ingest.Rejected) as exc:
        await ingest.ingest_files(
            [ingest.IngestFile(b"MZ", "application/x-msdownload")], source="photo", chat_id=1
        )
    assert "application/x-msdownload" in str(exc.value)
    assert db.list_board() == []


async def test_voice_transcribes_then_structures(fake_llm, monkeypatch):
    monkeypatch.setattr("app.transcribe.transcribe_remote", lambda _a: "напомни полить цветы")
    result = await ingest.ingest_audio(b"audio-bytes", chat_id=5)
    assert result.transcript == "напомни полить цветы"
    assert result.reminder.source == "voice"
    assert result.reminder.raw_text == "напомни полить цветы"
    assert fake_llm["text"] == "напомни полить цветы"


async def test_voice_falls_back_to_local_model(fake_llm, monkeypatch):
    warned = []

    def remote_down(_a):
        raise RuntimeError("OpenAI недоступен")

    monkeypatch.setattr("app.transcribe.transcribe_remote", remote_down)
    monkeypatch.setattr("app.transcribe.transcribe_local", lambda _a: "локально распознано")

    async def warn():
        warned.append(True)

    result = await ingest.ingest_audio(b"audio", chat_id=1, on_local_fallback=warn)
    assert result.transcript == "локально распознано"
    assert warned, "о медленном локальном проходе надо предупредить"


async def test_voice_failure_keeps_transcript_in_message(fake_llm, monkeypatch):
    """Если расшифровали, но не разобрали — расшифровку всё равно надо показать."""
    monkeypatch.setattr("app.transcribe.transcribe_remote", lambda _a: "что-то важное")
    monkeypatch.setattr(
        "app.llm.structure_text", lambda _t: (_ for _ in ()).throw(RuntimeError("нет"))
    )
    with pytest.raises(ingest.Failed) as exc:
        await ingest.ingest_audio(b"audio", chat_id=1)
    assert "что-то важное" in exc.value.user_message


async def test_scheduler_hook_is_called_for_saved_card(fake_llm):
    """Карточка из API должна получать напоминания так же, как присланная в чат."""
    scheduled = []
    ingest.set_scheduler(scheduled.append)
    result = await ingest.ingest_text("напомни завтра", chat_id=1)
    assert [r.id for r in scheduled] == [result.reminder.id]


async def test_broken_scheduler_does_not_lose_the_card(fake_llm):
    def boom(_reminder):
        raise RuntimeError("job queue упала")

    ingest.set_scheduler(boom)
    result = await ingest.ingest_text("купить хлеб", chat_id=1)
    assert db.get_reminder(result.reminder.id) is not None
