"""Audited HTTP reads and bounded Anthropic-compatible model calls."""

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .models import ModelSettings, Question, Recall, Recipe
from .storage import digest, read_json, write_json


class ReadOnlyAPI:
    def __init__(self, recipe: Recipe):
        self.recipe = recipe

    async def recall(self, question: Question, facts: int, chunks: int) -> Recall:
        body = {
            "query": question.query,
            "budget": "high",
            "max_tokens": facts,
            "prefer_observations": False,
            "trace": True,
            "include": {
                "entities": None,
                "chunks": {"max_tokens": chunks} if chunks else None,
            },
        }
        if question.query_timestamp:
            body["query_timestamp"] = question.query_timestamp
        identity = digest({"bank_id": question.bank_id, "request": body})
        cache = self.recipe.output / "recalls" / (identity + ".json")
        if cache.exists():
            saved = read_json(cache)
            if saved["bank_id"] != question.bank_id or saved["request"] != body:
                raise ValueError("Recall cache identity mismatch")
            return Recall.model_validate(saved["response"])
        if self.recipe.api_keys_file_env:
            key = read_json(Path(os.environ[self.recipe.api_keys_file_env]))[
                question.bank_id
            ]
        else:
            key = os.environ[self.recipe.api_key_env]
        path = (
            "/v1/default/banks/" + quote(question.bank_id, safe="") + "/memories/recall"
        )
        start = time.monotonic()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300)
        ) as session:
            async with session.post(
                self.recipe.api_url.rstrip("/") + path,
                json=body,
                headers={"Authorization": "Bearer " + key},
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"Recall HTTP {response.status}")
                raw = await response.json()
        result = Recall.model_validate(raw)
        write_json(
            cache,
            {
                "bank_id": question.bank_id,
                "request": body,
                "response": raw,
                "seconds": time.monotonic() - start,
            },
        )
        return result


class Models:
    def __init__(self, root: Path, max_calls: int):
        self.root = root
        self.max_calls = max_calls
        self.calls = 0
        self.previous_calls = len(list((root / "models").glob("*/started.json")))
        self.lock = asyncio.Lock()

    async def call(
        self,
        role: str,
        settings: ModelSettings,
        prompt: str,
        schema: dict | None = None,
        request_id: str = "",
    ) -> str | dict:
        body = {
            "model": settings.model,
            "max_tokens": settings.max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
            **settings.extra_body,
        }
        if schema:
            body["tools"] = [
                {
                    "name": "response",
                    "description": "Return the requested response.",
                    "input_schema": {"type": "object", **schema},
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": "response"}
        identity = digest(
            {
                "role": role,
                "request_id": request_id,
                "body": body,
                "endpoint": os.environ[settings.base_url_env],
            }
        )
        folder = self.root / "models" / identity
        path = folder / "result.json"
        if path.exists():
            return read_json(path)["parsed"]
        # An unresolved physical request must be reconciled, never silently repeated.
        if (folder / "started.json").exists():
            raise RuntimeError(
                "Unresolved/failed model attempt; inspect its recorded response before resuming"
            )
        async with self.lock:
            if self.previous_calls + self.calls >= self.max_calls:
                raise RuntimeError("Model call limit reached")
            self.calls += 1
            write_json(folder / "request.json", {"role": role, "body": body})
            write_json(
                folder / "started.json", {"unix": time.time(), "attempt": self.calls}
            )
        key = os.environ[settings.api_key_env]
        url = os.environ[settings.base_url_env].rstrip("/") + "/v1/messages"
        start = time.monotonic()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=240)
            ) as session:
                async with session.post(
                    url,
                    json=body,
                    headers={
                        "x-api-key": key,
                        "Authorization": "Bearer " + key,
                        "anthropic-version": "2023-06-01",
                    },
                ) as response:
                    raw = await response.json()
                    write_json(
                        folder / "raw.json",
                        {"status": response.status, "response": raw},
                    )
                    if response.status != 200:
                        raise RuntimeError(f"Model HTTP {response.status}")
            if raw.get("stop_reason") == "max_tokens":
                raise ValueError("Model output truncated")
            if schema:
                blocks = [
                    x["input"] for x in raw["content"] if x.get("type") == "tool_use"
                ]
                if len(blocks) != 1:
                    raise ValueError("Expected one structured tool result")
                parsed = blocks[0]
                if not all(k in parsed for k in schema["required"]):
                    raise ValueError("Missing structured response fields")
            else:
                parsed = "".join(
                    x.get("text", "") for x in raw["content"] if x.get("type") == "text"
                ).strip()
                if not parsed:
                    raise ValueError("Empty model answer")
            write_json(
                path,
                {
                    "parsed": parsed,
                    "usage": raw.get("usage"),
                    "model": raw.get("model"),
                    "seconds": time.monotonic() - start,
                },
            )
            return parsed
        except Exception as exc:
            write_json(
                folder / "error.json",
                {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "billing": "not assumed zero",
                },
            )
            raise
