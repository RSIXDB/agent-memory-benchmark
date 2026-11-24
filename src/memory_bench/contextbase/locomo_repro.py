"""LoCoMo #112-style reproduction with persistent banks and first-response checkpoints.

Uses AMB's unchanged LoCoMo loader, answer prompt and judge prompt. This module
never resets a bank, rewrites a question, selects answers by score or deletes data.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import time
import types
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .guards import exclusive_run
from .storage import read_json, write_json

DATA_SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
MISSION = (
    "Preserve the concrete details of the conversation, including explicit future plans, intentions, "
    "preferences, motivations, quantities, named items, places, and exceptions. When a short reply answers "
    "the preceding speaker's question, include that question's topic and time frame in the fact so the "
    "reply remains understandable on its own. Resolve pronouns to the correct speaker. Preserve the "
    "original relative date expression and any explicit weekday alongside a normalized date; if a date "
    "is ambiguous, retain that ambiguity rather than inventing precision. Keep different dated events "
    "separate. Preserve supplied image captions and descriptions when they carry factual details. "
    "Do not add facts or conclusions not supported by the conversation."
)
READER_PROPERTIES = {
    "reasoning": {
        "type": "string",
        "description": "Step-by-step explanation of how you used the context to arrive at the answer.",
    },
    "answer": {
        "type": "string",
        "description": "The final concise answer to the question. If the context lacks the information, say so.",
    },
}
JUDGE_PROPERTIES = {"correct": {"type": "boolean"}, "reason": {"type": "string"}}
BANK_CONFIG = {
    "retain_extraction_mode": "verbose",
    "retain_mission": MISSION,
    "enable_observations": False,
    "enable_auto_consolidation": False,
}


class LoCoMoRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output: Path
    api_url: str
    bank_prefix: str = Field(pattern=r"^[a-z0-9_]{8,38}$")
    data_path_env: str = "LOCOMO_DATA_PATH"
    api_key_env: str = "CONTEXTBASE_READ_KEY"
    model_base_url_env: str = "CONTEXTBASE_MODEL_URL"
    model_key_env: str = "CONTEXTBASE_MODEL_KEY"
    model_path: str = "/v1/chat/completions"
    reader_model: str = "zhipu/glm-5.3"
    judge_model: str = "qwen4-max"
    reader_max_tokens: int | None = Field(default=32768, ge=256, le=131072)
    judge_max_tokens: int | None = Field(default=4096, ge=256, le=131072)
    concurrency: int = Field(default=4, ge=1, le=12)
    format_retries: int = Field(default=1, ge=0, le=2)
    ingest_doc_id_metadata: bool = False

    def bank(self, user_id: str) -> str:
        if not re.fullmatch(r"conv-\d+", user_id):
            raise ValueError("Expected a canonical LoCoMo conversation ID")
        return self.bank_prefix + "_" + user_id.replace("-", "_")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def upstream_dataset():
    # The registry imports unrelated dataset integrations. A standalone recipe
    # loads the same module without requiring those optional integrations.
    if "memory_bench.dataset" not in sys.modules:
        package = types.ModuleType("memory_bench.dataset")
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "dataset")]
        sys.modules["memory_bench.dataset"] = package
    return importlib.import_module("memory_bench.dataset.locomo").LoComoDataset()


def parse_completion(raw: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Decode one unique JSON result, preserving answer bytes and strict booleans."""
    if raw.get("choices"):
        choice = raw["choices"][0]
        if choice.get("finish_reason") not in ("stop", "length", None):
            raise ValueError("Incomplete model transport")
        text = choice["message"]["content"]
    else:
        if raw.get("stop_reason") not in (
            "end_turn",
            "stop_sequence",
            "max_tokens",
            None,
        ):
            raise ValueError("Incomplete model transport")
        text = "".join(
            b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text"
        )
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    def normalized(value):
        if isinstance(value, list) and len(value) == 1:
            return normalized(value[0])
        if not isinstance(value, dict):
            return None
        if (
            required == ["reasoning", "answer"]
            and isinstance(value.get("answer"), str)
            and isinstance(value.get("reasoning", ""), str)
        ):
            return {**value, "reasoning": value.get("reasoning", "")}
        if (
            required == ["correct", "reason"]
            and type(value.get("correct")) is bool
            and isinstance(value.get("reason"), str)
        ):
            return value
        for wrapper in ("response", "result", "output"):
            if len(value) == 1 and wrapper in value:
                return normalized(value[wrapper])
        return None

    try:
        result = normalized(json.loads(text))
        if result is not None:
            return result
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    candidates = []
    for position, char in enumerate(text):
        if char != "{":
            continue
        try:
            item, _ = decoder.raw_decode(text[position:])
        except json.JSONDecodeError:
            continue
        result = normalized(item)
        if result is not None and result not in candidates:
            candidates.append(result)
    if len(candidates) != 1:
        raise ValueError("No unique response matching the requested schema")
    return candidates[0]


class Campaign:
    def __init__(self, recipe: LoCoMoRecipe):
        self.recipe = recipe
        self.root = recipe.output.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            raise ValueError("Campaign output directory must be private (0700)")
        self.new_calls = 0
        self.max_calls = 0
        self.model_lock = asyncio.Lock()
        parsed = urlsplit(recipe.api_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Credentials belong in headers, never API URLs")

    def prepare(self) -> None:
        source = Path(os.environ[self.recipe.data_path_env])
        if hashlib.sha256(source.read_bytes()).hexdigest() != DATA_SHA256:
            raise ValueError("Canonical LoCoMo dataset checksum does not match")
        os.environ["LOCOMO_DATA_PATH"] = str(source)
        dataset = upstream_dataset()
        documents = [asdict(d) for d in dataset.load_documents("locomo10")]
        questions = dataset.load_queries("locomo10")
        if len(documents) != 272 or len(questions) != 1540:
            raise ValueError("Expected272 source sessions and1540 questions")
        signature = {
            "recipe": self.recipe.model_dump(mode="json"),
            "dataset_sha256": DATA_SHA256,
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "loader_sha256": hashlib.sha256(
                Path(sys.modules[dataset.__module__].__file__).read_bytes()
            ).hexdigest(),
        }
        path = self.root / "signature.private.json"
        if path.exists() and read_json(path) != signature:
            raise ValueError(
                "Campaign configuration changed; use a new output directory"
            )
        write_json(path, signature)
        write_json(self.root / "documents.private.json", documents)
        write_json(
            self.root / "questions.private.json",
            [
                {
                    "id": q.id,
                    "query": q.query,
                    "user_id": q.user_id,
                    "bank_id": self.recipe.bank(q.user_id),
                    "meta": {k: v for k, v in q.meta.items() if k != "category"},
                }
                for q in questions
            ],
        )
        write_json(
            self.root / "evaluation.private.json",
            {
                q.id: {"gold_answers": q.gold_answers, "category": q.meta["category"]}
                for q in questions
            },
        )

    async def api(
        self, session, method: str, path: str, body=None, strict_create=False
    ):
        headers = {"Authorization": "Bearer " + os.environ[self.recipe.api_key_env]}
        if strict_create:
            headers["If-None-Match"] = "*"
        async with session.request(
            method, self.recipe.api_url.rstrip("/") + path, json=body, headers=headers
        ) as response:
            if response.status >= 400:
                raise RuntimeError(
                    f"ContextBase HTTP {response.status}; inspect the server-side operation"
                )
            return await response.json()

    async def ingest(self, allow_retain: bool) -> None:
        if not allow_retain:
            raise ValueError(
                "Ingestion requires --allow-retain; evaluation always reuses data"
            )
        docs = read_json(self.root / "documents.private.json")
        owned_path = self.root / "owned-banks.private.json"
        owned = read_json(owned_path) if owned_path.exists() else {}
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120)
        ) as session:
            for user in sorted({d["user_id"] for d in docs}):
                bank = self.recipe.bank(user)
                if bank not in owned:
                    response = await self.api(
                        session,
                        "PUT",
                        f"/v1/default/banks/{bank}",
                        {"name": bank, "enable_observations": False},
                        strict_create=True,
                    )
                    owned[bank] = {"created": time.time(), "response": response}
                    write_json(owned_path, owned)
                    await self.api(
                        session,
                        "PATCH",
                        f"/v1/default/banks/{bank}/config",
                        {"updates": BANK_CONFIG},
                    )
                config = await self.api(
                    session, "GET", f"/v1/default/banks/{bank}/config"
                )
                actual = config.get("config", config)
                if any(actual.get(key) != value for key, value in BANK_CONFIG.items()):
                    raise ValueError(
                        "Owned bank configuration does not match the frozen recipe"
                    )
            semaphore = asyncio.Semaphore(3)

            async def retain(doc):
                async with semaphore:
                    bank = self.recipe.bank(doc["user_id"])
                    operation = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            self.recipe.bank_prefix + "/" + doc["id"],
                        )
                    )
                    path = self.root / "ingestion" / (operation + ".private.json")
                    item = {
                        "content": doc["content"].replace("\x00", ""),
                        "document_id": doc["id"],
                    }
                    for key in ("timestamp", "context"):
                        if doc.get(key):
                            item[key] = doc[key]
                    if self.recipe.ingest_doc_id_metadata:
                        item["metadata"] = {"doc_id": doc["id"]}
                    body = {"items": [item], "async": True, "operation_id": operation}
                    if path.exists():
                        receipt = read_json(path)
                        if receipt["request_sha256"] != digest(body):
                            raise ValueError("Retain checkpoint input changed")
                        if not receipt.get("accepted"):
                            # Never blindly repeat an uncertain accepted write.
                            raise RuntimeError(
                                "Retain acknowledgement requires reconciliation"
                            )
                    else:
                        receipt = {
                            "bank_id": bank,
                            "document_id": doc["id"],
                            "request_sha256": digest(body),
                            "accepted": False,
                        }
                        write_json(path, receipt)
                        await self.api(
                            session, "POST", f"/v1/default/banks/{bank}/memories", body
                        )
                        receipt["accepted"] = True
                        write_json(path, receipt)
                    while True:
                        state = await self.api(
                            session,
                            "GET",
                            f"/v1/default/banks/{bank}/operations/{operation}",
                        )
                        status = state["status"]
                        if status == "completed":
                            write_json(
                                path, {**receipt, "status": status, "operation": state}
                            )
                            return
                        if status in ("failed", "cancelled") and not state.get(
                            "next_retry_at"
                        ):
                            write_json(
                                path, {**receipt, "status": status, "operation": state}
                            )
                            raise RuntimeError(
                                "Retain failed; inspect and use the server operation retry after auditing"
                            )
                        await asyncio.sleep(5)

            results = await asyncio.gather(
                *(retain(d) for d in docs), return_exceptions=True
            )
            if any(isinstance(x, BaseException) for x in results):
                raise RuntimeError(
                    "Ingestion incomplete; preserved every durable receipt"
                )

    async def audit(self) -> None:
        docs = read_json(self.root / "documents.private.json")
        checks = []
        grouped = {}
        for doc in docs:
            grouped.setdefault(doc["user_id"], set()).add(doc["id"])
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120)
        ) as session:
            for user, expected_ids in grouped.items():
                bank = self.recipe.bank(user)
                config = await self.api(
                    session, "GET", f"/v1/default/banks/{bank}/config"
                )
                effective = config.get("config", config)
                if any(effective.get(k) != v for k, v in BANK_CONFIG.items()):
                    raise ValueError("Bank configuration changed; audit refused")
                catalog = await self.api(
                    session, "GET", f"/v1/default/banks/{bank}/documents?limit=1000"
                )
                if (
                    catalog.get("total") != len(expected_ids)
                    or {d["id"] for d in catalog["items"]} != expected_ids
                ):
                    raise ValueError(
                        "Bank contains missing or unexpected source documents"
                    )
            for doc in docs:
                bank = self.recipe.bank(doc["user_id"])
                actual = await self.api(
                    session, "GET", f"/v1/default/banks/{bank}/documents/{doc['id']}"
                )
                text = actual.get("original_text")
                if text != doc["content"].replace("\x00", ""):
                    raise ValueError("Retained source mismatch: " + doc["id"])
                params = actual.get("retain_params") or {}
                if params.get("context") != doc.get("context"):
                    raise ValueError("Retained session context mismatch")
                if datetime.fromisoformat(
                    params["event_date"]
                ) != datetime.fromisoformat(doc["timestamp"]):
                    raise ValueError("Retained session date mismatch")
                expected_metadata = (
                    {"doc_id": doc["id"]} if self.recipe.ingest_doc_id_metadata else {}
                )
                if (
                    actual.get("document_metadata") or {}
                ) != expected_metadata or actual.get("tags"):
                    raise ValueError("Unexpected metadata or document tags")
                checks.append(
                    {
                        "bank_id": bank,
                        "document_id": doc["id"],
                        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    }
                )
        write_json(
            self.root / "source-audit.private.json",
            {
                "success": True,
                "signature_sha256": digest(
                    read_json(self.root / "signature.private.json")
                ),
                "documents": checks,
                "scope": "Original source content verified via REST; facts/vectors require the separate PG audit.",
            },
        )

    async def generate(
        self, session, qid: str, role: str, prompt: str, properties: dict
    ) -> dict:
        model = (
            self.recipe.reader_model if role == "reader" else self.recipe.judge_model
        )
        cap = (
            self.recipe.reader_max_tokens
            if role == "reader"
            else self.recipe.judge_max_tokens
        )
        schema = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
        }
        shaped = (
            prompt
            + "\n\nReturn exactly one JSON object matching this schema, with no Markdown: "
            + json.dumps(schema)
        )
        body = {
            "model": model,
            "messages": [{"role": "user", "content": shaped}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": {**schema, "additionalProperties": False},
                    "strict": True,
                },
            },
        }
        if cap is not None:
            body["max_tokens"] = cap
        endpoint = (
            os.environ[self.recipe.model_base_url_env].rstrip("/")
            + self.recipe.model_path
        )
        identity = digest({"endpoint": endpoint, "body": body})
        directory = self.root / "questions" / qid / role
        final = directory / "result.private.json"
        if final.exists():
            cached = read_json(final)
            if cached["identity"] != identity:
                raise ValueError("Model checkpoint differs from frozen request")
            return cached["parsed"]
        for attempt in range(self.recipe.format_retries + 1):
            folder = directory / str(attempt)
            rawpath = folder / "response.private.json.gz"
            if rawpath.exists():
                response = read_json(rawpath)
                if response["status"] != 200 and not (
                    response["status"] == 400
                    and response["body"].get("type") == "message"
                ):
                    raise RuntimeError(
                        "Recorded provider rejection/transport requires reconciliation; not resampling"
                    )
                raw = response["body"]
            else:
                if (folder / "started.private.json").exists():
                    raise RuntimeError(
                        "Unresolved physical model request; do not silently resample"
                    )
                async with self.model_lock:
                    if self.new_calls >= self.max_calls:
                        raise RuntimeError("Explicit model-call budget exhausted")
                    self.new_calls += 1
                    write_json(
                        folder / "request.private.json",
                        {"identity": identity, "body": body},
                    )
                    write_json(
                        folder / "started.private.json",
                        {"unix": time.time(), "ordinal": self.new_calls},
                    )
                key = os.environ[self.recipe.model_key_env]
                try:
                    async with session.post(
                        endpoint, json=body, headers={"Authorization": "Bearer " + key}
                    ) as result:
                        content = (await result.text()).replace(key, "<redacted>")
                        raw = json.loads(content)
                        saved = {"status": result.status, "body": raw}
                        rawpath.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with gzip.open(rawpath, "wt") as output:
                            json.dump(saved, output, ensure_ascii=False)
                        rawpath.chmod(0o600)
                        if result.status != 200 and not (
                            result.status == 400 and raw.get("type") == "message"
                        ):
                            raise RuntimeError(
                                "Provider rejected the request; no scored-answer retries"
                            )
                except Exception as error:
                    write_json(
                        folder / "error.private.json",
                        {"type": type(error).__name__, "billing": "not assumed zero"},
                    )
                    raise
            try:
                value = parse_completion(raw, list(properties))
            except ValueError:
                if not (
                    raw.get("type") == "message"
                    and raw.get("content")
                    or raw.get("choices")
                ):
                    raise RuntimeError(
                        "No generated completion; reconcile transport instead of resampling"
                    )
                if attempt == self.recipe.format_retries:
                    raise
                continue
            write_json(
                final,
                {
                    "identity": identity,
                    "parsed": value,
                    "request_ordinal": attempt,
                    "usage": raw.get("usage"),
                },
            )
            return value
        raise RuntimeError("No valid structured completion")

    async def evaluate(self, max_calls: int) -> None:
        audit = read_json(self.root / "source-audit.private.json")
        if not audit["success"] or audit["signature_sha256"] != digest(
            read_json(self.root / "signature.private.json")
        ):
            raise ValueError("A matching read-only source audit is required")
        if max_calls < 0:
            raise ValueError("Model-call budget cannot be negative")
        endpoint = (
            os.environ[self.recipe.model_base_url_env].rstrip("/")
            + self.recipe.model_path
        )
        parsed = urlsplit(endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Model credentials must use headers")
        execution = {
            "prepared_signature": digest(
                read_json(self.root / "signature.private.json")
            ),
            "model_endpoint": endpoint,
        }
        checkpoint = self.root / "execution.private.json"
        if checkpoint.exists() and read_json(checkpoint) != execution:
            raise ValueError(
                "Model endpoint or campaign identity changed; use a new output directory"
            )
        write_json(checkpoint, execution)
        self.max_calls = max_calls
        questions = read_json(self.root / "questions.private.json")
        dataset = upstream_dataset()
        semaphore = asyncio.Semaphore(self.recipe.concurrency)
        failures = []
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=900)
        ) as session:

            async def one(question):
                async with semaphore:
                    qid = question["id"]
                    final = self.root / "questions" / qid / "result.private.json"
                    if final.exists():
                        return
                    recallpath = final.parent / "recall.private.json.gz"
                    if recallpath.exists():
                        raw = read_json(recallpath)
                    else:
                        body = {
                            "query": question["query"][:1900],
                            "budget": "high",
                            "max_tokens": 32768,
                            "include": {
                                "entities": None,
                                "chunks": {"max_tokens": 16384},
                            },
                        }
                        if question["meta"].get("query_timestamp"):
                            body["query_timestamp"] = question["meta"][
                                "query_timestamp"
                            ]
                        raw = await self.api(
                            session,
                            "POST",
                            f"/v1/default/banks/{question['bank_id']}/memories/recall",
                            body,
                        )
                        recallpath.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with gzip.open(recallpath, "wt") as output:
                            json.dump(raw, output, ensure_ascii=False)
                        recallpath.chmod(0o600)
                    prompt = dataset.build_rag_prompt(
                        question["query"],
                        "",
                        "open",
                        "locomo10",
                        meta={**question["meta"], "_raw_response": raw},
                    )
                    answer = await self.generate(
                        session, qid, "reader", prompt, READER_PROPERTIES
                    )
                    # Gold is opened only after the persisted Reader completion.
                    evaluation = read_json(self.root / "evaluation.private.json")[qid]
                    decision = await self.generate(
                        session,
                        qid,
                        "judge",
                        dataset.build_judge_prompt(
                            question["query"],
                            evaluation["gold_answers"],
                            answer["answer"],
                        ),
                        JUDGE_PROPERTIES,
                    )
                    write_json(
                        final,
                        {
                            "query_id": qid,
                            "answer": answer["answer"],
                            "correct": decision["correct"],
                            "category": evaluation["category"],
                        },
                    )

            async def safe(question):
                try:
                    await one(question)
                except Exception as error:  # noqa: BLE001 - preserve all in-flight receipts before failing the run
                    failures.append(
                        {"query_id": question["id"], "error_type": type(error).__name__}
                    )

            await asyncio.gather(*(safe(q) for q in questions))
        rows = [
            read_json(self.root / "questions" / q["id"] / "result.private.json")
            for q in questions
            if (self.root / "questions" / q["id"] / "result.private.json").exists()
        ]
        full = len(rows) == 1540 and not failures
        write_json(
            self.root / "summary.private.json",
            {
                "expected": 1540,
                "valid": len(rows),
                "correct": sum(r["correct"] for r in rows),
                "complete_full_run": full,
                "accuracy": sum(r["correct"] for r in rows) / 1540 if full else None,
                "technical_failures": failures,
                "new_physical_model_calls": self.new_calls,
            },
        )
        if not full:
            raise RuntimeError("Incomplete run; no full-benchmark accuracy published")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "ingest", "audit", "evaluate"))
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--allow-retain", action="store_true")
    parser.add_argument("--max-new-model-calls", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        recipe = LoCoMoRecipe.model_validate(read_json(args.recipe))
    except ValidationError as error:
        parser.error(str(error.errors(include_input=False)))
    campaign = Campaign(recipe)
    with exclusive_run(campaign.root):
        campaign.prepare()
        if args.action == "ingest":
            asyncio.run(campaign.ingest(args.allow_retain))
        elif args.action == "audit":
            asyncio.run(campaign.audit())
        elif args.action == "evaluate":
            asyncio.run(campaign.evaluate(args.max_new_model_calls))


if __name__ == "__main__":
    main()
