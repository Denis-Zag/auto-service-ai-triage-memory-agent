import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings
from app.schemas import (
    AgentChatRequest,
    AgentChatResponse,
    Channel,
    TriageRequest,
)
from app.triage_service import StoredTriage, process_triage


logger = logging.getLogger(__name__)

AGENT_SYSTEM_PROMPT = """Ты — диалоговый помощник автосервиса.
Твоя задача — спокойно собрать сведения для первичного разбора обращения.

До отправки заявки выясни только факты, которые сообщил клиент:
1. автомобиль: марка и модель или хотя бы понятный тип автомобиля;
2. проблема: конкретные симптомы или вопрос клиента;
3. когда появилась проблема и при каких обстоятельствах;
4. дополнительные важные сведения, только если клиент сообщил их сам.

Задавай по одному короткому уточняющему вопросу за раз. Не выдумывай сведения,
цены, сроки, диагноз и выполненные работы. Если из описания следует возможная
опасность движения, посоветуй не продолжать поездку и обратиться к специалисту.

Дополнительные сведения не обязательны: не задавай о них отдельный вопрос.
Когда автомобиль, проблема и время появления собраны, сразу вызови tool
submit_triage_request ровно один раз. До этого не вызывай его.

Всегда возвращай структурированный результат:
- message: ответ клиенту на русском языке;
- status: collecting, пока данных не хватает, или submitted после tool;
- missing_fields: краткие названия недостающих сведений;
- ticket_id и triage: только после успешного сохранения заявки.
"""

ProcessTriage = Callable[[TriageRequest, Settings], Awaitable[StoredTriage]]


def make_checkpoint_thread_id(client_id: str, thread_id: str) -> str:
    """Создаёт непересекающийся идентификатор памяти без открытых ID клиента."""

    value = f"{client_id}\x1f{thread_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def compose_service_request(
    vehicle: str,
    problem: str,
    onset: str,
    additional_details: str = "",
) -> str:
    """Собирает проверенный текст обращения для существующего триажа."""

    parts = [
        f"Автомобиль: {vehicle.strip()}.",
        f"Проблема: {problem.strip()}.",
        f"Когда появилась: {onset.strip()}.",
    ]
    if additional_details.strip():
        parts.append(f"Дополнительные сведения: {additional_details.strip()}.")
    return " ".join(parts)


async def submit_service_request(
    *,
    vehicle: str,
    problem: str,
    onset: str,
    additional_details: str,
    client_id: str,
    channel: Channel,
    settings: Settings,
    processor: ProcessTriage = process_triage,
) -> dict[str, Any]:
    """Передаёт собранные агентом сведения в существующий сервис триажа."""

    text = compose_service_request(vehicle, problem, onset, additional_details)
    request = TriageRequest(text=text, channel=channel, client_id=client_id)
    stored = await processor(request, settings)
    return {
        "ticket_id": stored.ticket_id,
        "triage": stored.response.model_dump(mode="json"),
    }


def build_submit_tool(
    request: AgentChatRequest,
    settings: Settings,
):
    """Создаёт tool с зафиксированными ID клиента и каналом обращения."""

    from langchain.tools import tool

    @tool(return_direct=True)
    async def submit_triage_request(
        vehicle: str,
        problem: str,
        onset: str,
        additional_details: str = "",
    ) -> dict[str, Any]:
        """Сохраняет заявку после получения автомобиля, проблемы и времени появления.

        Вызывай функцию только после того, как пользователь сообщил все три
        обязательных значения. Не придумывай отсутствующие сведения.
        """

        return await submit_service_request(
            vehicle=vehicle,
            problem=problem,
            onset=onset,
            additional_details=additional_details,
            client_id=request.client_id,
            channel=request.channel,
            settings=settings,
        )

    return submit_triage_request


def submitted_response_from_messages(messages: list[Any]) -> AgentChatResponse | None:
    """Строит финальный ответ из результата реально выполненного tool-вызова."""

    for message in reversed(messages):
        if getattr(message, "name", None) != "submit_triage_request":
            continue

        content = getattr(message, "content", None)
        if isinstance(content, dict):
            payload = content
        elif isinstance(content, str):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                continue
        else:
            continue

        ticket_id = payload.get("ticket_id")
        triage = payload.get("triage")
        if ticket_id is None or triage is None:
            continue
        return AgentChatResponse(
            message=f"Спасибо, заявка №{ticket_id} сохранена и передана на первичный разбор.",
            status="submitted",
            missing_fields=[],
            ticket_id=ticket_id,
            triage=triage,
        )
    return None


def build_agent(
    request: AgentChatRequest,
    settings: Settings,
    checkpointer: Any,
):
    """Собирает LangChain-агента из модели, tool, схемы и checkpointer."""

    from langchain.agents import create_agent
    from langchain.agents.structured_output import ToolStrategy
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.proxyapi_key,
        base_url=settings.proxyapi_base_url,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
    )
    return create_agent(
        model=model,
        tools=[build_submit_tool(request, settings)],
        system_prompt=AGENT_SYSTEM_PROMPT,
        response_format=ToolStrategy(AgentChatResponse),
        checkpointer=checkpointer,
    )


async def run_agent_turn(
    request: AgentChatRequest,
    settings: Settings,
) -> AgentChatResponse:
    """Выполняет один шаг диалога с постоянной SQLite-памятью."""

    if not settings.proxyapi_key:
        raise RuntimeError("Переменная PROXYAPI_KEY не задана")

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    settings.agent_memory_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_thread_id = make_checkpoint_thread_id(
        request.client_id,
        request.thread_id,
    )
    config = {"configurable": {"thread_id": checkpoint_thread_id}}

    async with AsyncSqliteSaver.from_conn_string(
        str(settings.agent_memory_path)
    ) as checkpointer:
        agent = build_agent(request, settings, checkpointer)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": request.message}]},
            config=config,
        )

    submitted_response = submitted_response_from_messages(result.get("messages", []))
    if submitted_response is not None:
        return submitted_response

    structured_response = result.get("structured_response")
    if structured_response is None:
        raise RuntimeError("Агент не вернул структурированный ответ")
    return AgentChatResponse.model_validate(structured_response)


def agent_error_response() -> AgentChatResponse:
    """Возвращает понятный ответ, если диалоговый агент недоступен."""

    return AgentChatResponse(
        message=(
            "Сейчас не удалось продолжить автоматический сбор обращения. "
            "Передайте информацию оператору автосервиса."
        ),
        status="error",
        missing_fields=[],
    )
