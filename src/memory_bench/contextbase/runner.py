"""Reuse-only execution; evaluation inputs are never passed to retrieval or Reader."""

import asyncio
import json
from pathlib import Path

from . import beam, lme_v7, prompts
from .guards import (
    RunIntegrityError,
    environment_identity,
    exclusive_run,
    validate_evaluations,
)
from .models import Evaluation, Question, Recipe
from .storage import SourceReader, digest, read_json, write_json
from .transport import Models, ReadOnlyAPI

ANSWER_SCHEMA = {
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "Step-by-step explanation of how you used the context to arrive at the answer.",
        },
        "answer": {
            "type": "string",
            "description": "The final concise answer to the question. If the context lacks the information, say so.",
        },
    },
    "required": ["reasoning", "answer"],
}
REWRITE_SCHEMA = {
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "Explain the search decomposition without guessing historical facts.",
        },
        "answer": {
            "type": "string",
            "description": "A JSON array of exactly two self-contained search query strings.",
        },
    },
    "required": ["reasoning", "answer"],
}
JUDGE_SCHEMA = {
    "properties": {
        "score": {"type": "number", "description": "Score: 0.0, 0.5, or 1.0"},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
}


async def context(
    recipe: Recipe,
    question: Question,
    source: SourceReader,
    api: ReadOnlyAPI,
    models: Models,
) -> str:
    source.require_bank(question)
    baseline = await api.recall(question, recipe.facts_budget, recipe.chunks_budget)
    if recipe.method == "lme-v7":
        rows = await source.read(question)
        result = lme_v7.assemble(
            question.query,
            [x.model_dump() for x in rows],
            baseline.model_dump(mode="json"),
            recipe.context_budget,
        )
        write_json(recipe.output / "evidence" / (digest(question.id) + ".json"), result)
        return result["text"]
    if recipe.method == "beam-multiquery":
        rewritten = await models.call(
            "rewriter",
            recipe.rewriter,
            beam.REWRITE_PREFIX + question.query,
            REWRITE_SCHEMA,
            request_id=question.id,
        )
        queries = beam.validate_queries(json.loads(rewritten["answer"]))
        extra = []
        for query in queries:
            supplementary = question.model_copy(update={"query": query})
            extra.append(await api.recall(supplementary, 6144, 4096))
        selected = beam.merge(baseline, extra, recipe.context_budget)
        write_json(
            recipe.output / "evidence" / (digest(question.id) + ".json"),
            {
                "queries": queries,
                "document_ids": [x.id for x in selected],
                "context_cap_cl100k": recipe.context_budget,
            },
        )
        return beam.render(selected)
    if recipe.dataset == "longmemeval-s":
        return json.dumps(baseline.model_dump(mode="json"))
    return beam.render(beam.documents(baseline))


async def run(recipe: Recipe, limit: int | None, max_calls: int) -> None:
    with exclusive_run(recipe.output):
        await _run_locked(recipe, limit, max_calls)


async def _run_locked(recipe: Recipe, limit: int | None, max_calls: int) -> None:
    source = SourceReader(recipe, require_sealed=True)
    questions = [
        Question.model_validate(json.loads(line))
        for line in recipe.questions.read_text().splitlines()
        if line
    ]
    if len({q.id for q in questions}) != len(questions):
        raise ValueError("Duplicate question IDs")
    if limit:
        questions = questions[:limit]
    if not questions:
        raise ValueError("No questions")
    for question in questions:
        source.require_bank(question)
    validate_evaluations(recipe, questions)
    identity = environment_identity(recipe)
    await source.verify_database()
    if recipe.method == "lme-v7":
        for question in questions:
            # Verify current source before cached results or model calls.
            await source.read(question)
    protocol = {
        "environment": identity,
        "recipe": recipe.model_dump(mode="json"),
        "question_ids": [q.id for q in questions],
        "inputs_sha256": digest(recipe.questions.read_text()),
        "evaluation_sha256": digest(recipe.evaluation.read_text()),
        "audit_sha256": digest(recipe.audit_report.read_text()),
        "max_model_calls": max_calls,
        "source_hashes": {
            p.name: digest(p.read_text())
            for p in sorted(Path(__file__).parent.glob("*.py"))
        },
    }
    lock = recipe.output / "protocol.json"
    if lock.exists() and read_json(lock) != protocol:
        raise ValueError("Run configuration changed; use a new output directory")
    write_json(lock, protocol)
    models, api = Models(recipe.output, max_calls), ReadOnlyAPI(recipe)
    sem = asyncio.Semaphore(recipe.concurrency)

    async def one(question: Question):
        async with sem:
            target = recipe.output / "items" / (digest(question.id) + ".json")
            if target.exists():
                return read_json(target)
            record = {"id": question.id, "score": 0.0, "status": "failed"}
            try:
                evidence = await context(recipe, question, source, api, models)
                meta = (
                    {"query_timestamp": question.query_timestamp}
                    if question.query_timestamp
                    else {}
                )
                if recipe.dataset == "longmemeval-s":
                    prompt = prompts.lme_reader(
                        question.query, evidence, "open", "s", meta=meta
                    )
                    answer = await models.call(
                        "reader", recipe.reader, prompt, request_id=question.id
                    )
                else:
                    prompt = prompts.beam_reader(
                        question.query, evidence, "open", "10m"
                    )
                    parsed = await models.call(
                        "reader",
                        recipe.reader,
                        prompt,
                        ANSWER_SCHEMA,
                        request_id=question.id,
                    )
                    answer = parsed["answer"]
                write_json(
                    recipe.output / "answers" / (digest(question.id) + ".json"),
                    {
                        "id": question.id,
                        "answer": answer,
                        "context": evidence,
                        "reader_prompt": prompt,
                    },
                )
                # Evaluation data cannot participate in retrieval, rewrite or Reader prompt construction.
                evaluation = Evaluation.model_validate(
                    read_json(recipe.evaluation)[question.id]
                )
                if recipe.dataset == "longmemeval-s":
                    judge_prompt = prompts.get_anscheck_prompt(
                        evaluation.category,
                        question.query,
                        evaluation.answer,
                        answer,
                        evaluation.abstention,
                    )
                    verdict = await models.call(
                        "judge", recipe.judge, judge_prompt, request_id=question.id
                    )
                    score = float("yes" in verdict.lower())
                    record["strict_yes_no"] = verdict.strip().lower() in ("yes", "no")
                    record["judge_response"] = verdict
                else:
                    rubric = evaluation.rubric or [
                        "LLM response should contain: " + evaluation.answer
                    ]
                    if evaluation.category == "event_ordering":
                        rubric = [
                            r
                            if "should" in r.lower()
                            else "LLM response should mention: " + r
                            for r in rubric
                        ]
                    scores = []
                    for item_index, item in enumerate(rubric):
                        response = await models.call(
                            "judge",
                            recipe.judge,
                            prompts.beam_judge(question.query, answer, item),
                            JUDGE_SCHEMA,
                            request_id=f"{question.id}:{item_index}",
                        )
                        raw_score = float(response["score"])
                        scores.append(
                            1.0
                            if raw_score >= 0.75
                            else 0.5
                            if raw_score >= 0.25
                            else 0.0
                        )
                    score = sum(scores) / len(scores)
                    record["rubric_scores"] = scores
                record.update(status="complete", score=score, answer=answer)
            except RunIntegrityError:
                raise
            except Exception as exc:
                record.update(error_type=type(exc).__name__, error=str(exc))
            write_json(target, record)
            return record

    results = await asyncio.gather(*(one(q) for q in questions))
    summary = {
        "dataset": recipe.dataset,
        "method": recipe.method,
        "expected": len(questions),
        "complete": sum(r["status"] == "complete" for r in results),
        "failures": sum(r["status"] != "complete" for r in results),
        "score": sum(r["score"] for r in results) / len(questions),
        "new_physical_model_calls": models.calls,
        "results": results,
    }
    write_json(recipe.output / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}))
    if summary["failures"]:
        raise RuntimeError(
            "Run contains failed questions; inspect summary.json (failures remain in denominator)"
        )
