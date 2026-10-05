import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.schemas import (
    AgentChatResponse,
    AgentStatus,
    Category,
    Confidence,
    TriageResponse,
)
from app.triage_service import StoredTriage


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Не позволяет тестам создавать рабочую базу или вызывать модель."""

    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "triage.db"))
    get_settings.cache_clear()


def test_health() -> None:
    """Служебная проверка подтверждает готовность приложения."""

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_triage_uses_fallback_and_saves_ticket(tmp_path, monkeypatch) -> None:
    """При отключённой модели результат эскалируется и сохраняется."""

    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "triage.db"))
    get_settings.cache_clear()

    import importlib
    import app.main

    main = importlib.reload(app.main)
    with TestClient(main.app) as client:
        response = client.post(
            "/triage",
            json={
                "text": "После ремонта снова появился стук",
                "channel": "chat",
                "client_id": "test-client",
            },
        )

    assert response.status_code == 200
    assert response.json()["confidence"] == "low"
    assert response.json()["escalate"] is True
    assert "передано оператору" in response.json()["draft_reply"]
    with sqlite3.connect(main.settings.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    assert count == 1


def test_triage_rejects_empty_text() -> None:
    """Пустое обращение не проходит валидацию контракта."""

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/triage",
            json={"text": "", "channel": "chat", "client_id": "client"},
        )
    assert response.status_code == 422


def test_triage_rejects_whitespace_text() -> None:
    """Строка из пробелов считается пустым обращением."""

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/triage",
            json={"text": "   ", "channel": "chat", "client_id": "client"},
        )
    assert response.status_code == 422


def test_triage_returns_valid_llm_result(tmp_path, monkeypatch) -> None:
    """Корректный структурированный ответ модели проходит через API."""

    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "triage.db"))
    get_settings.cache_clear()

    import importlib
    import app.main

    main = importlib.reload(app.main)
    expected = TriageResponse(
        category=Category.COMPLAINT,
        draft_reply="Передадим обращение мастеру для повторной проверки.",
        confidence=Confidence.HIGH,
        escalate=True,
    )
    monkeypatch.setattr(
        main,
        "process_triage",
        AsyncMock(return_value=StoredTriage(response=expected, ticket_id=1)),
    )

    with TestClient(main.app) as client:
        response = client.post(
            "/triage",
            json={
                "text": "После ремонта снова появился стук",
                "channel": "chat",
                "client_id": "prompt-test-client",
            },
        )

    assert response.status_code == 200
    assert response.json() == expected.model_dump(mode="json")


def test_agent_chat_returns_structured_result(tmp_path, monkeypatch) -> None:
    """Диалоговый endpoint возвращает результат агента без реального API."""

    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "triage.db"))
    monkeypatch.setenv("AGENT_MEMORY_PATH", str(tmp_path / "agent_memory.db"))
    get_settings.cache_clear()

    import importlib
    import app.main

    main = importlib.reload(app.main)
    expected = AgentChatResponse(
        message="Уточните, когда появилась неисправность.",
        status=AgentStatus.COLLECTING,
        missing_fields=["время появления"],
    )
    monkeypatch.setattr(
        main,
        "run_agent_turn",
        AsyncMock(return_value=expected),
    )

    with TestClient(main.app) as client:
        response = client.post(
            "/agent/chat",
            json={
                "client_id": "agent-client",
                "thread_id": "dialog-1",
                "message": "У меня Lada Vesta, появился стук",
            },
        )

    assert response.status_code == 200
    assert response.json() == expected.model_dump(mode="json")


def test_agent_chat_isolates_invalid_empty_message() -> None:
    """Пустое сообщение не попадает в память агента."""

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/agent/chat",
            json={
                "client_id": "agent-client",
                "thread_id": "dialog-1",
                "message": "   ",
            },
        )

    assert response.status_code == 422


def test_agent_chat_returns_safe_error_when_llm_disabled(
    tmp_path,
    monkeypatch,
) -> None:
    """Отключённая модель не приводит к падению диалогового endpoint."""

    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "triage.db"))
    get_settings.cache_clear()

    import importlib
    import app.main

    main = importlib.reload(app.main)
    with TestClient(main.app) as client:
        response = client.post(
            "/agent/chat",
            json={
                "client_id": "agent-client",
                "thread_id": "dialog-1",
                "message": "У автомобиля появился стук",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["ticket_id"] is None
