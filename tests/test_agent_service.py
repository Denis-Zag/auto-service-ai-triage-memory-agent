import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agent_service import (
    compose_service_request,
    make_checkpoint_thread_id,
    submitted_response_from_messages,
    submit_service_request,
)
from app.config import Settings
from app.schemas import (
    Category,
    Channel,
    Confidence,
    TriageResponse,
)
from app.triage_service import StoredTriage


def test_checkpoint_thread_id_separates_clients_and_dialogs() -> None:
    """Одинаковые названия диалогов разных клиентов не смешивают память."""

    first = make_checkpoint_thread_id("client-1", "dialog")
    second = make_checkpoint_thread_id("client-2", "dialog")
    third = make_checkpoint_thread_id("client-1", "another-dialog")

    assert first != second
    assert first != third
    assert len(first) == 64
    assert "client-1" not in first


def test_compose_service_request_uses_only_provided_fields() -> None:
    """Текст заявки собирается из явно переданных пользователем сведений."""

    result = compose_service_request(
        vehicle="Lada Vesta",
        problem="стук при повороте",
        onset="сегодня утром",
        additional_details="горит индикатор ABS",
    )

    assert "Автомобиль: Lada Vesta" in result
    assert "Проблема: стук при повороте" in result
    assert "Когда появилась: сегодня утром" in result
    assert "горит индикатор ABS" in result


def test_submit_service_request_reuses_existing_triage() -> None:
    """Собственный tool передаёт собранный текст в существующий триаж."""

    triage = TriageResponse(
        category=Category.SUPPORT,
        draft_reply="Передадим описание мастеру для первичной проверки.",
        confidence=Confidence.HIGH,
        escalate=False,
    )
    processor = AsyncMock(
        return_value=StoredTriage(response=triage, ticket_id=17)
    )

    result = asyncio.run(
        submit_service_request(
            vehicle="Lada Vesta",
            problem="стук при повороте",
            onset="сегодня утром",
            additional_details="",
            client_id="client-1",
            channel=Channel.CHAT,
            settings=Settings(),
            processor=processor,
        )
    )

    assert result["ticket_id"] == 17
    assert result["triage"]["category"] == "support"
    request = processor.await_args.args[0]
    assert request.client_id == "client-1"
    assert request.channel == Channel.CHAT
    assert "Lada Vesta" in request.text


def test_submitted_agent_response_requires_saved_triage() -> None:
    """Статус завершения нельзя вернуть без реально сохранённой заявки."""

    from app.schemas import AgentChatResponse

    with pytest.raises(ValidationError):
        AgentChatResponse(
            message="Заявка сохранена.",
            status="submitted",
            missing_fields=[],
        )


def test_submitted_response_is_built_from_completed_tool_call() -> None:
    """После сохранения заявки агент не запускает лишний цикл модели."""

    from langchain_core.messages import ToolMessage

    message = ToolMessage(
        content=(
            '{"ticket_id": 17, "triage": {"category": "support", '
            '"draft_reply": "Передадим описание мастеру.", '
            '"confidence": "high", "escalate": false}}'
        ),
        tool_call_id="call-1",
        name="submit_triage_request",
    )

    response = submitted_response_from_messages([message])

    assert response is not None
    assert response.status == "submitted"
    assert response.ticket_id == 17
    assert response.triage is not None
    assert response.triage.category == "support"
