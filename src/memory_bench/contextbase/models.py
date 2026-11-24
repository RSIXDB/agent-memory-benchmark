"""Typed inputs separate retrieval data from evaluation-only answers and rubrics."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .evidence_client import EnhancedMemoryOptions


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    query: str
    bank_id: str
    query_timestamp: str | None = None


class Evaluation(BaseModel):
    answer: str
    category: str = ""
    abstention: bool = False
    rubric: list[str] = Field(default_factory=list)


class SourceDocument(BaseModel):
    id: str
    original_text: str
    retain_params: dict[str, Any]


class Memory(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    text: str
    type: str | None = None
    occurred_start: str | None = None
    occurred_end: str | None = None
    mentioned_at: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None


class Recall(BaseModel):
    model_config = ConfigDict(extra="allow")
    results: list[Memory]
    chunks: dict[str, dict[str, Any]] | None = None


class EvidenceDocument(BaseModel):
    id: str
    content: str


class ModelSettings(BaseModel):
    model: str
    base_url_env: str = "CONTEXTBASE_MODEL_URL"
    api_key_env: str = "CONTEXTBASE_MODEL_KEY"
    max_tokens: int = Field(default=16384, gt=0)
    extra_body: dict[str, Any] = Field(default_factory=dict)


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: Literal["longmemeval-s", "beam10m"]
    method: Literal["baseline", "lme-v7", "beam-multiquery", "source-evidence-v3", "native-sdk-json"]
    questions: Path
    evaluation: Path
    audit_report: Path
    environment_report: Path | None = None
    output: Path
    api_url: str
    api_key_env: str = "CONTEXTBASE_READ_KEY"
    api_keys_file_env: str | None = None
    postgres_dsn_env: str = "CONTEXTBASE_SOURCE_DSN"
    source: Literal["postgres", "json"] = "postgres"
    source_directory: Path | None = None
    facts_budget: int = Field(default=32768, gt=0)
    chunks_budget: int = Field(default=16384, ge=0)
    context_budget: int = Field(default=48000, gt=0)
    reader: ModelSettings
    judge: ModelSettings
    rewriter: ModelSettings | None = None
    evidence_options: EnhancedMemoryOptions | None = None
    concurrency: int = Field(default=2, ge=1, le=16)

    @model_serializer(mode="wrap")
    def serialize_recipe(self, handler):
        data = handler(self)
        # Existing baseline/v7/multiquery checkpoints retain the exact old recipe
        # identity. An unused new optional field must not invalidate those runs.
        if self.evidence_options is None:
            data.pop("evidence_options", None)
        return data

    @model_validator(mode="after")
    def compatible_method(self):
        if self.method == "native-sdk-json" and self.dataset != "longmemeval-s":
            raise ValueError("native-sdk-json requires longmemeval-s")
        if self.method == "lme-v7" and self.dataset != "longmemeval-s":
            raise ValueError("lme-v7 requires longmemeval-s")
        if self.method == "beam-multiquery" and (
            self.dataset != "beam10m" or self.rewriter is None
        ):
            raise ValueError("beam-multiquery requires BEAM and rewriter settings")
        if self.method == "source-evidence-v3":
            if (
                self.evidence_options is None
                or self.evidence_options.max_tokens != self.context_budget
            ):
                raise ValueError(
                    "source-evidence-v3 requires explicit evidence_options matching context_budget"
                )
        elif self.evidence_options is not None:
            raise ValueError("evidence_options requires source-evidence-v3")
        if self.source == "json" and self.source_directory is None:
            raise ValueError("JSON sources require source_directory")
        return self
