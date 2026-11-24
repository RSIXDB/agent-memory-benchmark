"""Migrate private historical artifacts without importing their executable controllers."""

import datetime
import hashlib
import json
from pathlib import Path

from . import beam, lme_v7, prompts
from .models import Question, Recall, Recipe
from .storage import SourceReader, read_json, write_json


def parse_date(value: str) -> str:
    import re

    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", value).strip()
    for fmt in ["%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"]:
        try:
            return (
                datetime.datetime.strptime(cleaned, fmt)
                .replace(tzinfo=datetime.timezone.utc)
                .isoformat()
            )
        except ValueError:
            pass
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()


def prepare(root: Path, dataset: str, audit_directory: Path, output: Path) -> None:
    """Export question/gold separation and private recipe; never ingest or copy source histories."""
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    evaluations = {}
    questions = []
    if dataset == "longmemeval-s":
        for row in read_json(root / "reference/longmemeval_s_mirror.json"):
            qid = row["question_id"]
            receipt = read_json(
                root / "candidates/concise-low/ingestion" / qid / "accepted.json"
            )
            questions.append(
                Question(
                    id=qid,
                    query=row["question"],
                    bank_id=receipt["workspace"]["bank_id"],
                    query_timestamp=parse_date(row["question_date"]),
                )
            )
            evaluations[qid] = {
                "answer": row["answer"],
                "category": row["question_type"],
                "abstention": "_abs" in qid,
            }
        audit = audit_directory / "longmemeval-reuse-audit.json"
        method, facts, chunks, context, port = "lme-v7", 32768, 16384, 48000, 8918
        reader = {
            "model": "glm-5.3",
            "max_tokens": 16384,
            "extra_body": {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high"},
            },
        }
    else:
        entries = read_json(root / "conversation-runs.json")
        bank_keys = {}
        for entry in entries:
            bank = read_json(Path(entry["root"]) / "frozen-bank.json")["bank"]
            bank_keys[bank] = read_json(Path(entry["keys"]))["read"]
            for row in read_json(Path(entry["corpus"]).parent / "questions.json"):
                questions.append(
                    Question(id=row["id"], query=row["query"], bank_id=bank)
                )
                evaluations[row["id"]] = {
                    "answer": (row["gold_answers"] or [""])[0],
                    "category": row["meta"]["question_category"],
                    "rubric": row["meta"].get("rubric", []),
                }
        write_json(output / "bank-keys.private.json", bank_keys)
        audit = audit_directory / "beam10m-reuse-audit.json"
        method, facts, chunks, context, port = (
            "beam-multiquery",
            12288,
            8192,
            60000,
            8916,
        )
        reader = {"model": "volcengine/deepseek-v4.1-flash", "max_tokens": 16384}
    questions_path = output / "questions.jsonl"
    questions_path.write_text("".join(q.model_dump_json() + "\n" for q in questions))
    evaluation_path = output / "evaluation.json"
    write_json(evaluation_path, evaluations)
    config = {
        "dataset": dataset,
        "method": method,
        "questions": str(questions_path),
        "evaluation": str(evaluation_path),
        "audit_report": str(audit),
        "output": str(output / "live"),
        "api_url": f"http://127.0.0.1:{port}",
        "facts_budget": facts,
        "chunks_budget": chunks,
        "context_budget": context,
        "reader": reader,
        "judge": {"model": "volcengine/deepseek-v4.1-flash", "max_tokens": 8192},
    }
    if dataset == "beam10m":
        config["api_keys_file_env"] = "CONTEXTBASE_BANK_KEYS_FILE"
        config["rewriter"] = {
            "model": "volcengine/deepseek-v4.1-flash",
            "max_tokens": 8192,
        }
    write_json(output / "recipe.private.json", config)
    print(
        json.dumps(
            {
                "dataset": dataset,
                "questions": len(questions),
                "recipe": str(output / "recipe.private.json"),
            }
        )
    )


async def replay(recipe: Recipe, historical_root: Path, output: Path) -> None:
    """Rebuild every final context from sources/recalls; never reuse answers to choose evidence."""
    source = SourceReader(recipe)
    questions = [
        Question.model_validate_json(x)
        for x in recipe.questions.read_text().splitlines()
        if x
    ]
    checks = []
    if recipe.dataset == "beam10m":
        roots = {}
        for entry in read_json(historical_root / "conversation-runs.json"):
            root = Path(entry["root"])
            for item in (root / "evaluation-multiquery-i").glob("*/result.json"):
                roots[read_json(item)["question_id"]] = item.parent
    for question in questions:
        source.require_bank(question)
        if recipe.dataset == "longmemeval-s":
            folder = (
                historical_root
                / "candidates/concise-low/rounds/full500-source-compact-v7"
                / question.id
            )
            rows = await source.read(question)
            raw = read_json(folder / "recall.json.gz")
            evidence = lme_v7.assemble(
                question.query,
                [x.model_dump() for x in rows],
                raw["response"],
                recipe.context_budget,
            )
            actual = evidence["text"]
            expected = read_json(folder / "context.json")["text"]
            prompt = prompts.lme_reader(
                question.query,
                actual,
                "open",
                "s",
                meta={"query_timestamp": question.query_timestamp},
            )
            prompt_equal = prompt == read_json(folder / "reader/request.json")["prompt"]
            answer_record = read_json(folder / "score.json")
            historical_score = float(answer_record["correct"])
        else:
            folder = roots[question.id]
            evidence = read_json(folder / "recall.json")
            baseline_file = (
                folder.parent.parent
                / "evaluation-baseline"
                / folder.name
                / "recall.json"
            )
            if (
                hashlib.sha256(baseline_file.read_bytes()).hexdigest()
                != evidence["baseline_recall_sha256"]
            ):
                raise ValueError("Historical baseline recall changed")
            baseline = Recall.model_validate(read_json(baseline_file)["response"])
            extra = [
                Recall.model_validate(x["response"]) for x in evidence["extra_recalls"]
            ]
            beam.validate_queries(evidence["queries"])
            if evidence["rewrite_prompt"] != beam.REWRITE_PREFIX + question.query:
                raise ValueError("Historical rewrite received unexpected input")
            selected = beam.merge(baseline, extra, recipe.context_budget)
            actual = beam.render(selected)
            record = read_json(folder / "result.json")
            expected = record["context"]
            prompt_equal = [d.id for d in selected] == evidence[
                "displayed_document_ids"
            ]
            historical_score = record["score"]
        checks.append(
            {
                "id": question.id,
                "context_equal": actual == expected,
                "prompt_or_ids_equal": prompt_equal,
                "context_sha256": hashlib.sha256(actual.encode()).hexdigest(),
                "historical_score": historical_score,
            }
        )
        if len(checks) % 50 == 0:
            print(json.dumps({"replay_checked": len(checks)}), flush=True)
    report = {
        "dataset": recipe.dataset,
        "questions": len(checks),
        "model_calls": 0,
        "all_contexts_equal": all(x["context_equal"] for x in checks),
        "all_prompts_or_ids_equal": all(x["prompt_or_ids_equal"] for x in checks),
        "historical_score_recount": sum(x["historical_score"] for x in checks)
        / len(checks),
        "scope": "Material reconstruction and historical score recount; not fresh model scoring",
        "checks": checks,
    }
    write_json(output, report)
    print(json.dumps({k: v for k, v in report.items() if k != "checks"}))
    if not report["all_contexts_equal"] or not report["all_prompts_or_ids_equal"]:
        raise ValueError(
            "Historical replay mismatch; do not claim equivalent reproduction"
        )
