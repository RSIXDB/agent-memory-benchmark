import asyncio
import json

import pytest
from pydantic import ValidationError

from memory_bench.contextbase.locomo_repro import (
    JUDGE_PROPERTIES,
    Campaign,
    LoCoMoRecipe,
    parse_completion,
)
from memory_bench.contextbase.storage import write_json


def message(text, stop="end_turn"):
    return {
        "type": "message",
        "stop_reason": stop,
        "content": [{"type": "text", "text": text}],
    }


def test_schema_echo_is_not_mistaken_for_a_second_answer():
    raw = message(
        '{"type":"object","properties":{"answer":{"type":"string"},"reasoning":{"type":"string"}}}\n\n{"reasoning":"source evidence","answer":"Tuesday"}'
    )
    assert parse_completion(raw, ["reasoning", "answer"])["answer"] == "Tuesday"


def test_conflicting_complete_objects_are_not_selected_by_correctness():
    with pytest.raises(ValueError, match="unique"):
        parse_completion(
            message('{"answer":"Tuesday"}\n{"answer":"Friday"}'),
            ["reasoning", "answer"],
        )


def test_only_auxiliary_reasoning_can_be_missing_and_answer_is_verbatim():
    result = parse_completion(
        message('```json\n{"response":{"answer":"Exact Original Answer!"}}\n```'),
        ["reasoning", "answer"],
    )
    assert result == {"answer": "Exact Original Answer!", "reasoning": ""}
    with pytest.raises(ValueError):
        parse_completion(
            message('{"correct":"false","reason":"no"}'), ["correct", "reason"]
        )


def test_complete_json_at_output_cap_is_kept_without_resampling():
    assert (
        parse_completion(
            message('{"answer":"kept"}', "max_tokens"), ["reasoning", "answer"]
        )["answer"]
        == "kept"
    )


def recipe(tmp_path):
    return LoCoMoRecipe(
        output=tmp_path / "private-run",
        api_url="http://127.0.0.1:1",
        bank_prefix="test_locomo",
    )


class FakeResponse:
    status = 200

    async def text(self):
        return json.dumps(message('{"correct":false,"reason":"first valid negative"}'))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class FakeSession:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return FakeResponse()


def test_first_valid_negative_judgment_is_cached_without_another_call(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CONTEXTBASE_MODEL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CONTEXTBASE_MODEL_KEY", "synthetic-unit-test-value")
    campaign = Campaign(recipe(tmp_path))
    campaign.max_calls = 3
    session = FakeSession()

    async def run():
        first = await campaign.generate(
            session, "q1", "judge", "unchanged prompt", JUDGE_PROPERTIES
        )
        second = await campaign.generate(
            session, "q1", "judge", "unchanged prompt", JUDGE_PROPERTIES
        )
        assert first == second and first["correct"] is False

    asyncio.run(run())
    assert session.calls == 1
    assert campaign.new_calls == 1


def test_unresolved_physical_attempt_is_never_silently_repeated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTBASE_MODEL_URL", "http://127.0.0.1:1")
    campaign = Campaign(recipe(tmp_path))
    campaign.max_calls = 100
    session = FakeSession()
    write_json(campaign.root / "questions/q1/judge/0/started.private.json", {"unix": 1})
    with pytest.raises(RuntimeError, match="Unresolved"):
        asyncio.run(campaign.generate(session, "q1", "judge", "p", JUDGE_PROPERTIES))
    assert session.calls == 0


def test_ingestion_needs_explicit_opt_in_and_namespaces_cannot_escape(tmp_path):
    campaign = Campaign(recipe(tmp_path))
    with pytest.raises(ValueError, match="allow-retain"):
        asyncio.run(campaign.ingest(False))
    with pytest.raises(ValueError):
        campaign.recipe.bank("../../existing-bank")
    with pytest.raises(ValidationError):
        LoCoMoRecipe(output=tmp_path, api_url="http://localhost", bank_prefix="x" * 70)


def test_enhanced_recipe_is_explicit_and_native_recipe_serialization_stays_compatible(
    tmp_path,
):
    native = recipe(tmp_path)
    assert "enhanced_memory" not in native.model_dump(mode="json")
    assert "evidence_options" not in native.model_dump(mode="json")
    enhanced = LoCoMoRecipe(**{**native.model_dump(), "enhanced_memory": True})
    assert enhanced.evidence_options.assembly == "native_first"
    assert enhanced.evidence_options.max_tokens == 64000
    with pytest.raises(ValidationError):
        LoCoMoRecipe(
            **{**native.model_dump(), "evidence_options": {"max_tokens": 64000}}
        )


def test_enhanced_evaluation_uses_packed_context_without_raw_recall_override(
    tmp_path, monkeypatch
):
    from memory_bench.contextbase.evidence_client import EvidenceClient
    from memory_bench.contextbase.locomo_repro import digest

    monkeypatch.setenv("CONTEXTBASE_MODEL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CONTEXTBASE_READ_KEY", "synthetic-test-only")
    campaign = Campaign(
        LoCoMoRecipe(**{**recipe(tmp_path).model_dump(), "enhanced_memory": True})
    )
    signature = {"synthetic": "dispatch-test"}
    write_json(campaign.root / "signature.private.json", signature)
    write_json(
        campaign.root / "source-audit.private.json",
        {"success": True, "signature_sha256": digest(signature)},
    )
    write_json(
        campaign.root / "questions.private.json",
        [{"id": "q", "query": "question", "bank_id": "b", "meta": {}}],
    )
    write_json(
        campaign.root / "evaluation.private.json",
        {"q": {"gold_answers": ["reference"], "category": 1}},
    )
    captured = {}

    class Dataset:
        def build_rag_prompt(self, query, context, *args, meta):
            captured.update(context=context, meta=meta)
            return "reader prompt"

        def build_judge_prompt(self, *args):
            return "judge prompt"

    async def recall(self, bank, query, **kwargs):
        return {"evidence": {"context": "SOURCE MATERIAL"}}

    async def generate(*args):
        return (
            {"answer": "first"}
            if args[2] == "reader"
            else {"correct": False, "reason": "first valid negative"}
        )

    monkeypatch.setattr(EvidenceClient, "recall", recall)
    monkeypatch.setattr(
        "memory_bench.contextbase.locomo_repro.upstream_dataset", lambda: Dataset()
    )
    monkeypatch.setattr(campaign, "generate", generate)
    with pytest.raises(RuntimeError, match="Incomplete run"):
        asyncio.run(campaign.evaluate(0))
    assert (
        captured["context"] == "SOURCE MATERIAL"
        and "_raw_response" not in captured["meta"]
    )
    assert (
        json.loads((campaign.root / "questions/q/result.private.json").read_text())[
            "correct"
        ]
        is False
    )
