import asyncio
import json

import pytest
from pydantic import ValidationError

from memory_bench.contextbase.evidence_client import (
    EnhancedMemoryOptions,
    EvidenceClient,
    enhanced_memory_enabled,
    options_from_environment,
    retrieve_enhanced,
)


def test_enhancement_is_off_by_default_and_invalid_flags_fail(monkeypatch):
    monkeypatch.delenv("AMB_ENHANCED_MEMORY", raising=False)
    assert not enhanced_memory_enabled()
    monkeypatch.setenv("AMB_ENHANCED_MEMORY", "true")
    assert enhanced_memory_enabled()
    monkeypatch.setenv("AMB_ENHANCED_MEMORY", "yes")
    with pytest.raises(ValueError):
        enhanced_memory_enabled()


def test_options_are_explicit_and_cannot_name_a_benchmark(monkeypatch):
    monkeypatch.setenv(
        "AMB_ENHANCED_MEMORY_OPTIONS",
        json.dumps({"max_tokens": 12000, "independent_search": False}),
    )
    assert options_from_environment().max_tokens == 12000
    assert not options_from_environment().independent_search
    with pytest.raises(ValidationError):
        EnhancedMemoryOptions(benchmark="locomo")


def test_query_gate_and_fallback_are_not_silently_dropped():
    with pytest.raises(ValueError, match="query-gate"):
        asyncio.run(retrieve_enhanced(object(), "q", "alice", None, {"query_gate": {}}))


def test_provider_preserves_scope_and_returns_only_packed_context(monkeypatch):
    captured = {}

    class Provider:
        def _recall_kwargs(self, *args, **kwargs):
            return {
                "bank_id": "alice",
                "query": "question",
                "max_tokens": 32768,
                "tags": ["private"],
                "tags_match": "all_strict",
            }

        def _recall_endpoint(self):
            return "http://127.0.0.1:1", {"Authorization": "Bearer synthetic-test-key"}

    async def recall(self, bank_id, query, **kwargs):
        captured.update(bank_id=bank_id, query=query, **kwargs)
        return {
            "candidate_documents": ["original-1"],
            "evidence": {"context": "[user] literal evidence"},
        }

    monkeypatch.setattr(EvidenceClient, "recall", recall)
    documents, raw = asyncio.run(
        retrieve_enhanced(Provider(), "question", "alice", None, None)
    )
    assert captured["tags"] == ["private"] and captured["tags_match"] == "all_strict"
    assert documents[0].content == "[user] literal evidence"
    assert documents[0].source_ids == ["original-1"]
    assert raw is None


def test_reuse_runner_issues_one_evidence_request_and_preserves_the_cached_first_context(
    tmp_path, monkeypatch
):
    from memory_bench.contextbase.models import Question, Recipe
    from memory_bench.contextbase.transport import ReadOnlyAPI

    calls = []
    recipe = Recipe(
        dataset="beam10m",
        method="source-evidence-v3",
        questions=tmp_path / "questions",
        evaluation=tmp_path / "gold",
        audit_report=tmp_path / "audit",
        output=tmp_path,
        api_url="http://127.0.0.1:1",
        context_budget=1000,
        evidence_options={"max_tokens": 1000},
        reader={"model": "r", "max_tokens": 100},
        judge={"model": "j", "max_tokens": 100},
    )
    monkeypatch.setenv("CONTEXTBASE_READ_KEY", "synthetic-test-value")

    async def recall(self, bank, query, **kwargs):
        calls.append((bank, query, kwargs))
        return {"evidence": {"context": "unchanged first evidence"}}

    monkeypatch.setattr(EvidenceClient, "recall", recall)
    api = ReadOnlyAPI(recipe)
    question = Question(id="q", query="question", bank_id="bank")

    async def run():
        assert await api.source_evidence(question) == "unchanged first evidence"
        assert await api.source_evidence(question) == "unchanged first evidence"

    asyncio.run(run())
    assert len(calls) == 1
    with pytest.raises(ValidationError):
        recipe.model_copy(update={"context_budget": 2000}).model_validate(
            {**recipe.model_dump(), "context_budget": 2000}
        )


def test_explicitly_disabled_originals_are_not_overridden_by_the_enhancement():
    class Provider:
        def _recall_kwargs(self, *args, **kwargs):
            return {
                "bank_id": "b",
                "query": "q",
                "include_chunks": False,
                "max_tokens": 1000,
            }

        def _recall_endpoint(self):
            raise AssertionError(
                "Must refuse the conflicting configuration before HTTP"
            )

    with pytest.raises(ValueError, match="original-source access"):
        asyncio.run(retrieve_enhanced(Provider(), "q", "u", None, None))


def test_explicit_sdk_comparator_preserves_the_full_response_and_first_cache(tmp_path, monkeypatch):
    import asyncio
    import json
    import sys
    from types import SimpleNamespace

    from memory_bench.contextbase.models import Question, Recipe
    from memory_bench.contextbase.transport import ReadOnlyAPI

    requests = []
    closed = []
    raw = {"results": [{"id": "m", "text": "fact", "entities": None}], "trace": None, "chunks": {"c": {"text": "source"}}}

    class SDK:
        def __init__(self, **settings):
            assert settings["max_attempts"] == 1 and settings["timeout"] == 240

        async def arecall(self, **request):
            requests.append(request)
            return SimpleNamespace(model_dump=lambda **_: raw)

        async def aclose(self):
            closed.append(True)

    monkeypatch.setitem(sys.modules, "contextbase_client", SimpleNamespace(ContextBase=SDK))
    monkeypatch.setenv("CONTEXTBASE_READ_KEY", "synthetic")
    recipe = Recipe(dataset="longmemeval-s", method="native-sdk-json", questions=tmp_path / "q", evaluation=tmp_path / "e", audit_report=tmp_path / "a", output=tmp_path, api_url="http://127.0.0.1:1", reader={"model": "test"}, judge={"model": "test"})
    q = Question(id="q", query="question", bank_id="b", query_timestamp="2020-01-01T00:00:00Z")
    api = ReadOnlyAPI(recipe)
    first = asyncio.run(api.native_sdk_context(q))
    assert first == json.dumps(raw) and closed == [True]
    assert asyncio.run(api.native_sdk_context(q)) == first and len(requests) == 1
    assert requests[0] == {"bank_id": "b", "query": "question", "budget": "high", "max_tokens": 32768, "include_entities": False, "include_chunks": True, "max_chunk_tokens": 16384, "trace": False, "query_timestamp": q.query_timestamp}
