from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateApiKey(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=128)


class AppendMessage(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(max_length=1_048_576)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class CompactSession(StrictModel):
    summary: str = Field(max_length=1_048_576)
    through_message_id: int = Field(gt=0)


class MemoryWrite(StrictModel):
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, max_length=512)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    vector: list[float] | None = Field(default=None, max_length=8192)


class MemorySearch(StrictModel):
    query: str | None = Field(default=None, min_length=1, max_length=100_000)
    vector: list[float] | None = Field(default=None, max_length=8192)
    session_id: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=5, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def require_query_or_vector(self) -> "MemorySearch":
        if self.query is None and self.vector is None:
            raise ValueError("provide query text or an embedding vector")
        return self