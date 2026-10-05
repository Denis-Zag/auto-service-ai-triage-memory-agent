from enum import StrEnum
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Channel(StrEnum):
    """Допустимые метки источника обращения."""

    EMAIL = "email"
    FORM = "form"
    CHAT = "chat"


class Category(StrEnum):
    """Категории обращений из контракта задания."""

    BILLING = "billing"
    SUPPORT = "support"
    COMPLAINT = "complaint"
    OTHER = "other"


class Confidence(StrEnum):
    """Уровень уверенности классификации."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AgentStatus(StrEnum):
    """Этап диалога агента с клиентом."""

    COLLECTING = "collecting"
    SUBMITTED = "submitted"
    ERROR = "error"


class TriageRequest(BaseModel):
    """Входные данные для обработки обращения."""

    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=2000)
    channel: Channel
    client_id: str = Field(min_length=1, max_length=100)


class TriageResponse(BaseModel):
    """Структурированный результат обработки обращения."""

    model_config = ConfigDict(str_strip_whitespace=True)

    category: Category
    draft_reply: str = Field(min_length=1)
    confidence: Confidence
    escalate: bool

    @field_validator("draft_reply")
    @classmethod
    def validate_sentence_count(cls, value: str) -> str:
        """Проверяет ограничение от одного до шести предложений."""

        parts = [part for part in re.split(r"[.!?]+(?:\s+|$)", value) if part.strip()]
        sentence_count = len(parts) or 1
        if sentence_count > 6:
            raise ValueError("draft_reply должен содержать не больше 6 предложений")
        return value


class AgentChatRequest(BaseModel):
    """Сообщение клиента в отдельном диалоге с агентом."""

    model_config = ConfigDict(str_strip_whitespace=True)

    client_id: str = Field(min_length=1, max_length=100)
    thread_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    channel: Channel = Channel.CHAT


class AgentChatResponse(BaseModel):
    """Структурированный результат одного шага диалога с агентом."""

    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(min_length=1)
    status: AgentStatus
    missing_fields: list[str] = Field(default_factory=list)
    ticket_id: int | None = Field(default=None, ge=1)
    triage: TriageResponse | None = None

    @field_validator("missing_fields")
    @classmethod
    def remove_empty_missing_fields(cls, value: list[str]) -> list[str]:
        """Удаляет пустые и повторяющиеся названия недостающих полей."""

        result: list[str] = []
        for item in value:
            normalized = item.strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @model_validator(mode="after")
    def validate_status_payload(self) -> "AgentChatResponse":
        """Проверяет согласованность статуса, заявки и результата триажа."""

        if self.status == AgentStatus.SUBMITTED:
            if self.ticket_id is None or self.triage is None:
                raise ValueError(
                    "Для status=submitted нужны ticket_id и triage"
                )
            if self.missing_fields:
                raise ValueError(
                    "У сохранённой заявки не должно быть missing_fields"
                )
        elif self.ticket_id is not None or self.triage is not None:
            raise ValueError(
                "ticket_id и triage допустимы только для status=submitted"
            )
        return self
