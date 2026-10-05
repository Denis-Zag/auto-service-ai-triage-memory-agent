import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from app.agent_service import agent_error_response, run_agent_turn
from app.config import get_settings
from app.database import initialize_database
from app.rate_limiter import ClientRateLimiter
from app.schemas import (
    AgentChatRequest,
    AgentChatResponse,
    TriageRequest,
    TriageResponse,
)
from app.triage_service import process_triage


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()
rate_limiter = ClientRateLimiter(settings.requests_per_minute)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Подготавливает выбранное хранилище перед запуском API."""

    initialize_database(settings)
    yield


app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Подтверждает, что приложение запущено."""

    return {"status": "ok"}


@app.post("/triage", response_model=TriageResponse)
async def triage(request: TriageRequest) -> TriageResponse:
    """Классифицирует обращение и сохраняет результат в журнале."""

    if not rate_limiter.is_allowed(request.client_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Превышен лимит запросов для client_id",
        )

    logger.info(
        "Обработка обращения: client_id=%s channel=%s text_length=%d",
        request.client_id,
        request.channel.value,
        len(request.text),
    )
    stored = await process_triage(request, settings)
    return stored.response


@app.post("/agent/chat", response_model=AgentChatResponse)
async def agent_chat(request: AgentChatRequest) -> AgentChatResponse:
    """Продолжает диалог агента и сохраняет его состояние по thread_id."""

    if not rate_limiter.is_allowed(request.client_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Превышен лимит запросов для client_id",
        )

    logger.info(
        "Шаг диалога агента: client_id=%s thread_id=%s message_length=%d",
        request.client_id,
        request.thread_id,
        len(request.message),
    )
    try:
        if not settings.use_llm:
            raise RuntimeError("LLM отключена настройкой USE_LLM")
        return await run_agent_turn(request, settings)
    except Exception as error:
        logger.exception(
            "Ошибка диалогового агента: error_type=%s",
            type(error).__name__,
        )
        return agent_error_response()
