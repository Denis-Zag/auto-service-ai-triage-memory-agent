import logging
from dataclasses import dataclass

from app.config import Settings
from app.database import save_ticket
from app.llm_client import triage_with_llm
from app.schemas import Category, Confidence, TriageRequest, TriageResponse


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredTriage:
    """Результат классификации вместе с номером сохранённой заявки."""

    response: TriageResponse
    ticket_id: int
    error_name: str | None = None


def fallback_response() -> TriageResponse:
    """Возвращает безопасный ответ при недоступности модели."""

    return TriageResponse(
        category=Category.OTHER,
        draft_reply=(
            "Ваше обращение передано оператору автосервиса. "
            "Оператор уточнит информацию и свяжется с вами."
        ),
        confidence=Confidence.LOW,
        escalate=True,
    )


async def process_triage(
    request: TriageRequest,
    settings: Settings,
) -> StoredTriage:
    """Классифицирует обращение и сохраняет результат в журнале."""

    error_name = None
    try:
        if not settings.use_llm:
            raise RuntimeError("LLM отключена настройкой USE_LLM")
        response = await triage_with_llm(request, settings)
    except Exception as error:
        error_name = type(error).__name__
        logger.exception("Ошибка LLM: error_type=%s", error_name)
        response = fallback_response()

    ticket_id = save_ticket(settings, request, response, error_name)
    logger.info("Обращение сохранено: ticket_id=%d", ticket_id)
    return StoredTriage(
        response=response,
        ticket_id=ticket_id,
        error_name=error_name,
    )
