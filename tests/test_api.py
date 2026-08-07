"""JSON API для телефона. Главное, что проверяем: без токена не пускает, а с токеном
ведёт себя так же, как бот — те же правила захвата и те же статусы."""
import pytest
from fastapi.testclient import TestClient

from app import api, db, ingest, llm, tokens
from app.main import app

OWNER = 130359870


@pytest.fixture
def client():
    # https обязателен: сессионная кука выставляется только по защищённому соединению.
    return TestClient(app, base_url="https://test")


@pytest.fixture
def auth(client):
    """Заголовок с токеном устройства, полученным честным обменом кода."""
    code = tokens.issue_code(OWNER, "Борис")
    response = client.post(
        "/api/v1/pair", json={"code": tokens.format_code(code), "device_name": "iPhone"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
def fake_llm(monkeypatch):
    monkeypatch.setattr(
        "app.llm.structure_text", lambda text: llm.ExtractedReminder(title="Из текста")
    )
    monkeypatch.setattr(
        "app.llm.structure_files",
        lambda files: llm.ExtractedReminder(title=f"Из файлов: {len(files)}", category="ticket"),
    )
    monkeypatch.setattr("app.transcribe.transcribe_remote", lambda audio: "голосом сказано")


# --- Доступ -------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/v1/me"),
        ("get", "/api/v1/board"),
        ("get", "/api/v1/cards"),
        ("get", "/api/v1/devices"),
        ("post", "/api/v1/capture"),
    ],
)
def test_without_token_everything_is_401(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    # Клиенту нужен именно 401, а не редирект на HTML-страницу входа.
    assert response.headers["content-type"].startswith("application/json")


def test_bad_token_is_401(client):
    # Значение заголовка — только ASCII, иначе запрос не соберётся ещё на клиенте.
    response = client.get("/api/v1/board", headers={"Authorization": "Bearer no-such-token"})
    assert response.status_code == 401


def test_pair_with_wrong_code_is_400(client):
    assert client.post("/api/v1/pair", json={"code": "ZZZZ-ZZZZ"}).status_code == 400


def test_pair_code_works_once(client):
    code = tokens.issue_code(OWNER)
    assert client.post("/api/v1/pair", json={"code": code}).status_code == 200
    assert client.post("/api/v1/pair", json={"code": code}).status_code == 400


def test_me_confirms_the_token(client, auth):
    assert client.get("/api/v1/me", headers=auth).json()["user_id"] == OWNER


def test_dashboard_session_also_opens_api(client):
    """Дашборд в браузере ходит в те же эндпоинты — по куке, без второго механизма."""
    code = tokens.issue_code(OWNER, "Борис")
    client.post("/login/code", data={"code": code})
    assert client.get("/api/v1/board").status_code == 200


def test_device_can_be_listed_and_revoked(client, auth):
    devices = client.get("/api/v1/devices", headers=auth).json()
    assert [d["name"] for d in devices] == ["iPhone"]
    assert client.delete(f"/api/v1/devices/{devices[0]['id']}", headers=auth).status_code == 200
    assert client.get("/api/v1/board", headers=auth).status_code == 401


# --- Чтение -------------------------------------------------------------------


def test_board_has_three_columns_with_cards(client, auth):
    db.add_reminder(title="Дело", category="task", chat_id=OWNER)
    body = client.get("/api/v1/board", headers=auth).json()
    assert [c["key"] for c in body["columns"]] == ["todo", "doing", "done"]
    assert [c["title"] for c in body["columns"][0]["cards"]] == ["Дело"]
    assert body["server_time"].endswith("Z"), "время должно быть помечено как UTC"


def test_card_exposes_file_links(client, auth):
    reminder = db.add_reminder(title="Два билета", file_paths=["/a.pdf", "/b.pdf"])
    card = client.get(f"/api/v1/cards/{reminder.id}", headers=auth).json()
    assert card["files"] == [
        f"/api/v1/cards/{reminder.id}/files/0",
        f"/api/v1/cards/{reminder.id}/files/1",
    ]


def test_missing_card_is_404(client, auth):
    assert client.get("/api/v1/cards/4242", headers=auth).status_code == 404


def test_since_returns_only_what_changed(client, auth):
    old = db.add_reminder(title="Старая", chat_id=OWNER)
    cursor = client.get("/api/v1/board", headers=auth).json()["server_time"]
    fresh = db.add_reminder(title="Новая", chat_id=OWNER)

    changed = client.get(f"/api/v1/cards?since={cursor}", headers=auth).json()["cards"]
    assert [c["title"] for c in changed] == ["Новая"]

    # Изменение старой карточки тоже обязано попасть в разницу.
    db.set_status(old.id, "doing")
    titles = {
        c["title"] for c in client.get(f"/api/v1/cards?since={cursor}", headers=auth).json()["cards"]
    }
    assert titles == {"Новая", "Старая"}
    assert fresh.id != old.id


def test_since_includes_archived_so_client_can_drop_them(client, auth):
    reminder = db.add_reminder(title="Уехала в архив", chat_id=OWNER)
    cursor = client.get("/api/v1/board", headers=auth).json()["server_time"]
    db.set_status(reminder.id, "archived")
    cards = client.get(f"/api/v1/cards?since={cursor}", headers=auth).json()["cards"]
    assert [(c["title"], c["status"]) for c in cards] == [("Уехала в архив", "archived")]


def test_cards_can_be_filtered_by_status(client, auth):
    db.add_reminder(title="В работе", status="doing", chat_id=OWNER)
    db.add_reminder(title="Ждёт", status="todo", chat_id=OWNER)
    cards = client.get("/api/v1/cards?status=doing", headers=auth).json()["cards"]
    assert [c["title"] for c in cards] == ["В работе"]


def test_unknown_status_filter_is_400(client, auth):
    assert client.get("/api/v1/cards?status=мимо", headers=auth).status_code == 400


def test_file_is_served_and_missing_one_is_404(client, auth, tmp_path):
    ticket = tmp_path / "ticket.pdf"
    ticket.write_bytes(b"%PDF-real")
    reminder = db.add_reminder(title="Билет", file_paths=[str(ticket), "/нет/такого.pdf"])

    ok = client.get(f"/api/v1/cards/{reminder.id}/files/0", headers=auth)
    assert ok.status_code == 200 and ok.content == b"%PDF-real"
    # Файл записан в базе, но пропал с диска — это 404, а не 500.
    assert client.get(f"/api/v1/cards/{reminder.id}/files/1", headers=auth).status_code == 404
    assert client.get(f"/api/v1/cards/{reminder.id}/files/9", headers=auth).status_code == 404


# --- Изменение ----------------------------------------------------------------


def test_patch_moves_card_and_sets_importance(client, auth):
    reminder = db.add_reminder(title="Дело", chat_id=OWNER)
    body = client.patch(
        f"/api/v1/cards/{reminder.id}",
        json={"status": "done", "importance": 3, "remind_active": False},
        headers=auth,
    ).json()
    assert (body["status"], body["importance"], body["remind_active"]) == ("done", 3, False)
    # done-флаг для обратной совместимости должен остаться синхронным со статусом.
    assert db.get_reminder(reminder.id).done is True


def test_patch_validates_status_and_card(client, auth):
    reminder = db.add_reminder(title="Дело", chat_id=OWNER)
    assert client.patch(
        f"/api/v1/cards/{reminder.id}", json={"status": "мимо"}, headers=auth
    ).status_code == 400
    assert client.patch(
        "/api/v1/cards/4242", json={"status": "done"}, headers=auth
    ).status_code == 404


def test_patch_clamps_importance(client, auth):
    reminder = db.add_reminder(title="Дело", chat_id=OWNER)
    body = client.patch(
        f"/api/v1/cards/{reminder.id}", json={"importance": 99}, headers=auth
    ).json()
    assert body["importance"] == 3


# --- Захват -------------------------------------------------------------------


def test_capture_text(client, auth, fake_llm):
    body = client.post("/api/v1/capture", data={"text": "купить хлеб"}, headers=auth).json()
    assert body["card"]["title"] == "Из текста"
    assert body["card"]["source"] == "text"
    # chat_id = user id: карточка с телефона тоже должна получить пинг в Telegram.
    assert db.get_reminder(body["card"]["id"]).chat_id == OWNER


def test_capture_photo_saves_file(client, auth, fake_llm):
    response = client.post(
        "/api/v1/capture",
        files=[("files", ("ticket.jpg", b"\xff\xd8jpeg-bytes", "image/jpeg"))],
        headers=auth,
    )
    card = response.json()["card"]
    assert card["title"] == "Из файлов: 1" and card["source"] == "photo"
    assert client.get(card["files"][0], headers=auth).content == b"\xff\xd8jpeg-bytes"


def test_capture_album_makes_one_card(client, auth, fake_llm):
    response = client.post(
        "/api/v1/capture",
        files=[
            ("files", ("a.pdf", b"%PDF-a", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-b", "application/pdf")),
        ],
        headers=auth,
    )
    assert response.json()["card"]["title"] == "Из файлов: 2"
    assert response.json()["card"]["source"] == "pdf"
    assert len(db.list_board()) == 1


def test_capture_voice_returns_transcript(client, auth, fake_llm):
    body = client.post(
        "/api/v1/capture",
        files=[("files", ("note.m4a", b"audio-bytes", "audio/m4a"))],
        headers=auth,
    ).json()
    assert body["transcript"] == "голосом сказано"
    assert body["card"]["source"] == "voice"


def test_capture_rejects_unsupported_file(client, auth, fake_llm):
    response = client.post(
        "/api/v1/capture",
        files=[("files", ("app.exe", b"MZ", "application/x-msdownload"))],
        headers=auth,
    )
    assert response.status_code == 422  # осознанный отказ, а не сбой
    assert db.list_board() == []


def test_capture_needs_something(client, auth, fake_llm):
    assert client.post("/api/v1/capture", data={}, headers=auth).status_code == 400


def test_capture_reports_model_failure_as_502(client, auth, monkeypatch):
    def boom(_text):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.llm.structure_text", boom)
    response = client.post("/api/v1/capture", data={"text": "что-то"}, headers=auth)
    assert response.status_code == 502
    assert "Не смог разобрать" in response.json()["detail"]


def test_capture_refuses_oversized_file(client, auth, fake_llm, monkeypatch):
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 10)
    response = client.post(
        "/api/v1/capture",
        files=[("files", ("big.jpg", b"x" * 100, "image/jpeg"))],
        headers=auth,
    )
    assert response.status_code == 413


def test_capture_refuses_too_many_files(client, auth, fake_llm, monkeypatch):
    monkeypatch.setattr(api, "MAX_FILES", 1)
    response = client.post(
        "/api/v1/capture",
        files=[
            ("files", ("a.jpg", b"a", "image/jpeg")),
            ("files", ("b.jpg", b"b", "image/jpeg")),
        ],
        headers=auth,
    )
    assert response.status_code == 413


def test_capture_refuses_several_audio_files(client, auth, fake_llm):
    response = client.post(
        "/api/v1/capture",
        files=[
            ("files", ("a.m4a", b"a", "audio/m4a")),
            ("files", ("b.m4a", b"b", "audio/m4a")),
        ],
        headers=auth,
    )
    assert response.status_code == 400


def test_capture_schedules_reminders_like_the_bot(client, auth, fake_llm, monkeypatch):
    """Пинг «за день до» не должен зависеть от того, откуда пришла карточка."""
    scheduled = []
    ingest.set_scheduler(scheduled.append)
    monkeypatch.setattr(
        "app.llm.structure_text",
        lambda text: llm.ExtractedReminder(title="Концерт", event_date="2030-01-01"),
    )
    client.post("/api/v1/capture", data={"text": "концерт первого января 2030"}, headers=auth)
    assert [r.title for r in scheduled] == ["Концерт"]
