"""Experimental default-off client for ContextBase source-evidence recall.

This transport never sees benchmark gold, rewrites queries or invokes a model.
Unsupported filters fail closed instead of silently widening the caller's scope.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Literal
from urllib.parse import quote

import aiohttp
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ..models import Document


class EnhancedMemoryOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["source-evidence-v3"] = "source-evidence-v3"
    max_tokens: int = Field(default=64000, ge=256, le=120000)
    tokenizer: Literal["cl100k_base", "o200k_base"] = "o200k_base"
    max_documents: int = Field(default=128, ge=1, le=256)
    max_source_characters: int = Field(default=4000000, ge=1000, le=4000000)
    max_windows: int = Field(default=512, ge=1, le=1024)
    neighbor_messages: int = Field(default=1, ge=0, le=3)
    fact_fraction: float = Field(default=0.15, ge=0, le=0.5)
    user_fraction: float = Field(default=0.60, ge=0, le=0.9)
    assembly: Literal["ranked_windows", "user_coverage", "native_first"] = (
        "native_first"
    )
    independent_search: bool = True
    before: AwareDatetime | None = None


def enhanced_memory_enabled() -> bool:
    value = os.environ.get("AMB_ENHANCED_MEMORY", "false").lower()
    if value not in ("true", "false"):
        raise ValueError("AMB_ENHANCED_MEMORY must be true or false")
    return value == "true"


def options_from_environment(default_max_tokens: int = 64000) -> EnhancedMemoryOptions:
    values = json.loads(os.environ.get("AMB_ENHANCED_MEMORY_OPTIONS", "{}"))
    if not isinstance(values, dict):
        raise TypeError("AMB_ENHANCED_MEMORY_OPTIONS must be a JSON object")
    return EnhancedMemoryOptions.model_validate(
        {"max_tokens": default_max_tokens, **values}
    )


class EvidenceClient:
    def __init__(
        self, base_url: str, headers: dict[str, str], options: EnhancedMemoryOptions
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers)
        self.options = options

    async def recall(
        self,
        bank_id: str,
        query: str,
        *,
        max_tokens: int = 32768,
        query_timestamp: str | None = None,
        tags: list[str] | None = None,
        tags_match: str = "any",
        tag_groups: list[dict] | None = None,
    ) -> dict:
        body = {
            "query": query,
            "budget": "high",
            "max_tokens": max_tokens,
            "query_timestamp": query_timestamp,
            "tags": tags,
            "tags_match": tags_match,
            "tag_groups": tag_groups,
            "evidence": self.options.model_dump(mode="json"),
        }
        path = (
            "/v1/default/banks/" + quote(bank_id, safe="") + "/memories/recall-evidence"
        )
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session,
            session.post(
                self.base_url + path, headers=self.headers, json=body
            ) as response,
        ):
            if response.status == 404:
                raise RuntimeError(
                    "Source evidence is unavailable: enable the experimental server route explicitly"
                )
            if response.status != 200:
                raise RuntimeError(
                    f"Source-evidence HTTP {response.status}; inspect server/index setup"
                )
            result = await response.json()
        evidence = result.get("evidence", {})
        if evidence.get("version") != self.options.version:
            raise ValueError("Unexpected source-evidence algorithm version")
        if not isinstance(evidence.get("context"), str):
            raise TypeError("Source-evidence context is missing")
        if (
            type(evidence.get("tokens")) is not int
            or not 0 <= evidence["tokens"] <= self.options.max_tokens
        ):
            raise ValueError("Source-evidence token ceiling violated")
        import tiktoken

        actual = len(
            tiktoken.get_encoding(self.options.tokenizer).encode(
                evidence["context"], disallowed_special=()
            )
        )
        if actual != evidence["tokens"]:
            raise ValueError(
                "Source-evidence token count does not match the complete context"
            )
        if evidence.get("extra_generative_calls") != 0:
            raise ValueError(
                "Source-evidence route must not add generative model calls"
            )
        return result


async def retrieve_enhanced(
    provider,
    query: str,
    user_id: str | None,
    query_timestamp: str | None,
    filters: dict | None,
):
    if filters:
        raise ValueError(
            "Enhanced-memory provider currently requires explicit tags, without query-gate/fallback filters"
        )
    kwargs = provider._recall_kwargs(query, user_id, query_timestamp, filters=filters)
    unsupported = set(kwargs) - {
        "bank_id",
        "query",
        "budget",
        "max_tokens",
        "include_entities",
        "include_chunks",
        "max_chunk_tokens",
        "query_timestamp",
        "tags",
        "tags_match",
        "tag_groups",
    }
    if unsupported:
        raise ValueError(
            "Unsupported enhanced-memory recall fields: "
            + ", ".join(sorted(unsupported))
        )
    if kwargs.get("include_chunks") is False or kwargs.get("max_chunk_tokens") == 0:
        raise ValueError(
            "Enhanced evidence requires original-source access; chunk-disabled settings are incompatible"
        )
    if kwargs.get("include_entities"):
        raise ValueError("Extra entity output is not supported by enhanced evidence")
    base_url, headers = provider._recall_endpoint()
    client = EvidenceClient(base_url, headers, options_from_environment())
    result = await client.recall(
        kwargs["bank_id"],
        kwargs["query"],
        max_tokens=kwargs["max_tokens"],
        query_timestamp=kwargs.get("query_timestamp"),
        tags=kwargs.get("tags"),
        tags_match=kwargs.get("tags_match", "any"),
        tag_groups=kwargs.get("tag_groups"),
    )
    context = result["evidence"]["context"]
    identifier = "source-evidence:" + hashlib.sha256(context.encode()).hexdigest()
    # AMB's existing RAG prompt uses this context. Returning no raw recall JSON
    # prevents its dataset prompt from substituting the ordinary fact response.
    return [
        Document(
            id=identifier,
            content=context,
            user_id=user_id,
            source_ids=result["candidate_documents"],
        )
    ], None
