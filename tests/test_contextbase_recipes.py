"""Behavioral checks for identity, evidence preservation, isolation and call caps."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from pydantic import ValidationError

from memory_bench.contextbase import beam, lme_v7
from memory_bench.contextbase.guards import (
    environment_identity,
    exclusive_run,
    validate_evaluations,
)
from memory_bench.contextbase.models import ModelSettings, Question, Recall, Recipe
from memory_bench.contextbase.runner import run
from memory_bench.contextbase.storage import (
    RunIntegrityError,
    SourceReader,
    digest,
    source_fingerprint,
    write_json,
)
from memory_bench.contextbase.transport import Models, ReadOnlyAPI


def recipe(root: Path) -> Recipe:
    return Recipe(
        dataset="longmemeval-s",
        method="lme-v7",
        questions=root / "questions.jsonl",
        evaluation=root / "eval.json",
        audit_report=root / "audit.json",
        output=root / "out",
        api_url="http://127.0.0.1",
        reader=ModelSettings(model="reader"),
        judge=ModelSettings(model="judge"),
    )


class EvidenceTests(unittest.TestCase):
    def test_gold_cannot_enter_question(self):
        with self.assertRaises(ValidationError):
            Question(id="q", query="why", bank_id="b", answer="GOLD")

    def test_same_chunk_different_facts_survive(self):
        r = Recall.model_validate(
            {
                "results": [
                    {"id": "1", "text": "before", "chunk_id": "c"},
                    {"id": "2", "text": "after", "chunk_id": "c"},
                ],
                "chunks": {"c": {"text": "source"}},
            }
        )
        docs = beam.documents(r)
        self.assertEqual(len(docs), 2)
        self.assertEqual(beam.render(docs).count("> source"), 1)

    def test_beam_preserves_baseline_and_interleaves(self):
        def recall(ids):
            return Recall.model_validate(
                {"results": [{"id": i, "text": i} for i in ids]}
            )

        selected = beam.merge(
            recall(["base"]), [recall(["a", "base"]), recall(["b", "c"])]
        )
        self.assertEqual([x.id for x in selected], ["base", "a", "b", "c"])

    def test_beam_never_silently_truncates_baseline(self):
        r = Recall.model_validate(
            {"results": [{"id": "base", "text": "source " * 100}]}
        )
        empty = Recall(results=[])
        with self.assertRaises(ValueError):
            beam.merge(r, [empty, empty], budget=1)

    def test_rewrite_validation(self):
        for invalid in [["one"], ["", "two"], ["x" * 1201, "two"], {"answer": "guess"}]:
            with self.assertRaises(ValueError):
                beam.validate_queries(invalid)

    def test_lme_short_reply_keeps_question_context(self):
        rows = [
            {
                "id": "doc",
                "original_text": json.dumps(
                    [
                        {
                            "role": "assistant",
                            "content": "How many times did you visit Paris?",
                        },
                        {"role": "user", "content": "Twice."},
                    ]
                ),
                "retain_params": {"event_date": "2024-01-01"},
            }
        ]
        evidence = lme_v7.assemble("Paris visits", rows, {"results": []}, 48000)
        self.assertIn("How many times", evidence["text"])
        self.assertIn("Twice", evidence["text"])
        self.assertEqual(evidence["memory_lead_token_budget"], 0)

    def test_source_audit_blocks_wrong_bank(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_json(
                root / "audit.json",
                {
                    "dataset": "longmemeval-s",
                    "reusable": True,
                    "errors": [],
                    "checks": [{"bank_id": "b", "schema": "ctxdb_ws_b"}],
                },
            )
            source = SourceReader(recipe(root))
            with self.assertRaises(RunIntegrityError):
                source.require_bank(Question(id="q", query="why", bank_id="other"))

    def test_source_failed_audit_blocks_execution(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_json(
                root / "audit.json", {"dataset": "longmemeval-s", "reusable": False}
            )
            with self.assertRaises(ValueError):
                SourceReader(recipe(root))


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.requests = []
        app = web.Application()

        async def recall(request):
            self.requests.append({"path": request.path, "body": await request.json()})
            return web.json_response(
                {"results": [{"id": "m", "text": "evidence"}], "chunks": {}}
            )

        async def model(request):
            self.requests.append({"body": await request.json()})
            return web.json_response(
                {
                    "content": [{"type": "text", "text": "yes"}],
                    "usage": {"input_tokens": 1},
                    "model": "stub",
                    "stop_reason": "end_turn",
                }
            )

        app.router.add_post("/v1/default/banks/{bank}/memories/recall", recall)
        app.router.add_post("/v1/messages", model)
        self.server = web.AppRunner(app)
        await self.server.setup()
        site = web.TCPSite(self.server, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        os.environ["CONTEXTBASE_READ_KEY"] = "test"
        os.environ["CONTEXTBASE_MODEL_KEY"] = "test"
        os.environ["CONTEXTBASE_MODEL_URL"] = self.url

    async def asyncTearDown(self):
        await self.server.cleanup()
        self.temp.cleanup()

    async def test_rest_budget_scope_and_cache(self):
        cfg = recipe(self.root).model_copy(update={"api_url": self.url})
        api = ReadOnlyAPI(cfg)
        q = Question(id="q", query="why", bank_id="b", query_timestamp="2024-01-01")
        await api.recall(q, 123, 456)
        await api.recall(q, 123, 456)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("/banks/b/", self.requests[0]["path"])
        self.assertEqual(
            self.requests[0]["body"]["include"]["chunks"]["max_tokens"], 456
        )
        self.assertEqual(self.requests[0]["body"]["max_tokens"], 123)

    async def test_model_call_cap_and_cache(self):
        model = Models(self.root, 1)
        settings = ModelSettings(model="test")
        self.assertEqual(await model.call("reader", settings, "first"), "yes")
        self.assertEqual(await model.call("reader", settings, "first"), "yes")
        with self.assertRaises(RuntimeError):
            await model.call("reader", settings, "second")
        self.assertEqual(len(self.requests), 1)

    async def test_unresolved_model_call_not_retried(self):
        model = Models(self.root, 2)
        settings = ModelSettings(model="test")
        await model.call("reader", settings, "first")
        next((self.root / "models").glob("*/result.json")).unlink()
        with self.assertRaises(RuntimeError):
            await model.call("reader", settings, "first")
        self.assertEqual(len(self.requests), 1)

    async def test_model_cap_survives_resume(self):
        settings = ModelSettings(model="test")
        await Models(self.root, 1).call("reader", settings, "first")
        with self.assertRaises(RuntimeError):
            await Models(self.root, 1).call("reader", settings, "second")
        self.assertEqual(len(self.requests), 1)

    async def test_identical_prompts_for_different_questions_are_separate_calls(self):
        settings = ModelSettings(model="test")
        models = Models(self.root, 2)
        await models.call("reader", settings, "same", request_id="q1")
        await models.call("reader", settings, "same", request_id="q2")
        self.assertEqual(len(self.requests), 2)


class GuardTests(unittest.TestCase):
    def test_latest_failed_audit_blocks_stale_success(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_json(
                root / "audit.json",
                {
                    "dataset": "longmemeval-s",
                    "reusable": True,
                    "checks": [],
                },
            )
            write_json(root / "audit-run.json", {"failed_sections": ["lme"]})
            with self.assertRaises(RunIntegrityError):
                SourceReader(recipe(root))

    def test_only_matching_complete_sealed_audit_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "audit.json"
            write_json(
                path,
                {
                    "dataset": "longmemeval-s",
                    "reusable": True,
                    "checks": [],
                    "audit_run_id": "new",
                },
            )
            manifest = {
                "id": "new",
                "state": "complete",
                "reports": {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest(),
                },
            }
            write_json(root / "audit-run.json", manifest)
            SourceReader(recipe(root), require_sealed=True)
            manifest["id"] = "old"
            write_json(root / "audit-run.json", manifest)
            with self.assertRaises(RunIntegrityError):
                SourceReader(recipe(root), require_sealed=True)

    def test_source_content_changes_fail_even_when_count_matches(self):
        from memory_bench.contextbase.models import SourceDocument

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            row = SourceDocument(id="d", original_text="original", retain_params={})
            write_json(
                root / "audit.json",
                {
                    "dataset": "longmemeval-s",
                    "reusable": True,
                    "checks": [
                        {
                            "bank_id": "b",
                            "schema": "ctxdb_ws_b",
                            "source_sha256": source_fingerprint([row]),
                        }
                    ],
                },
            )
            reader = SourceReader(recipe(root))
            question = Question(id="q", query="why", bank_id="b")
            reader.verify_sources(question, [row])
            with self.assertRaises(RunIntegrityError):
                reader.verify_sources(
                    question, [row.model_copy(update={"original_text": "changed"})]
                )

    def test_directory_lock_excludes_other_process_and_releases(self):
        with tempfile.TemporaryDirectory() as d:
            command = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; from memory_bench.contextbase.guards import exclusive_run; "
                    "import sys; "
                    "lock=exclusive_run(Path(sys.argv[1])); lock.__enter__(); lock.__exit__(None,None,None)"
                ),
                d,
            ]
            with exclusive_run(Path(d)):
                result = subprocess.run(
                    command, check=False, capture_output=True, text=True
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Another process", result.stderr)
            self.assertEqual(
                subprocess.run(command, check=False, capture_output=True).returncode, 0
            )

    def test_unscorable_input_stops_before_any_score(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = recipe(root).model_copy(
                update={"dataset": "beam10m", "method": "baseline"}
            )
            q = Question(id="q", query="why", bank_id="b")
            write_json(root / "eval.json", {"q": {"answer": "", "rubric": []}})
            with self.assertRaises(RunIntegrityError):
                validate_evaluations(cfg, [q])
            self.assertFalse((cfg.output / "summary.json").exists())

    def test_environment_fingerprints_change_without_recording_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            report = root / "effective.json"
            write_json(report, {"source_commit": "fixed"})
            cfg = recipe(root).model_copy(update={"environment_report": report})
            from unittest.mock import patch

            with patch.dict(
                os.environ,
                {
                    "CONTEXTBASE_MODEL_URL": "https://old",
                    "CONTEXTBASE_SOURCE_DSN": "postgres://secret@old/db",
                },
            ):
                before = environment_identity(cfg)
                os.environ["CONTEXTBASE_MODEL_URL"] = "https://new"
                after = environment_identity(cfg)
                self.assertNotEqual(before, after)
                self.assertNotIn("secret", json.dumps(before))


class ResumeGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_beam_bank_stops_without_score_or_model_calls(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            q = Question(id="q", query="why", bank_id="not-audited")
            (root / "questions.jsonl").write_text(q.model_dump_json() + "\n")
            write_json(
                root / "audit.json",
                {
                    "dataset": "beam10m",
                    "reusable": True,
                    "audit_run_id": "run",
                    "checks": [{"bank_id": "b", "schema": "ctxdb_ws_b"}],
                },
            )
            write_json(
                root / "audit-run.json",
                {
                    "id": "run",
                    "state": "complete",
                    "reports": {
                        "audit.json": hashlib.sha256(
                            (root / "audit.json").read_bytes()
                        ).hexdigest(),
                    },
                },
            )
            cfg = recipe(root).model_copy(
                update={"dataset": "beam10m", "method": "baseline"}
            )
            with self.assertRaisesRegex(RunIntegrityError, "Question bank"):
                await run(cfg, None, 2)
            self.assertFalse((cfg.output / "models").exists())
            self.assertFalse((cfg.output / "summary.json").exists())

    async def test_endpoint_change_cannot_reuse_completed_items(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            q = Question(id="q", query="why", bank_id="b")
            (root / "questions.jsonl").write_text(q.model_dump_json() + "\n")
            write_json(root / "eval.json", {"q": {"answer": "expected"}})
            write_json(
                root / "audit.json",
                {
                    "dataset": "longmemeval-s",
                    "reusable": True,
                    "audit_run_id": "run",
                    "checks": [{"bank_id": "b", "schema": "ctxdb_ws_b"}],
                },
            )
            write_json(
                root / "audit-run.json",
                {
                    "id": "run",
                    "state": "complete",
                    "reports": {
                        "audit.json": hashlib.sha256(
                            (root / "audit.json").read_bytes()
                        ).hexdigest(),
                    },
                },
            )
            write_json(root / "effective.json", {"source_commit": "fixed"})
            cfg = recipe(root).model_copy(
                update={
                    "method": "baseline",
                    "source": "json",
                    "source_directory": root,
                    "environment_report": root / "effective.json",
                }
            )
            write_json(
                cfg.output / "items" / (digest(q.id) + ".json"),
                {"id": q.id, "status": "complete", "score": 1.0},
            )
            with patch.dict(os.environ, {"CONTEXTBASE_MODEL_URL": "https://old"}):
                await run(cfg, None, 2)
                protocol = (cfg.output / "protocol.json").read_bytes()
                summary = (cfg.output / "summary.json").read_bytes()
                os.environ["CONTEXTBASE_MODEL_URL"] = "https://new"
                with self.assertRaises(ValueError):
                    await run(cfg, None, 2)
                self.assertEqual((cfg.output / "protocol.json").read_bytes(), protocol)
                self.assertEqual((cfg.output / "summary.json").read_bytes(), summary)
                self.assertFalse((cfg.output / "models").exists())


if __name__ == "__main__":
    unittest.main()
