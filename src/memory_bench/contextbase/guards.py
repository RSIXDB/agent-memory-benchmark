"""Run guards only: never select evidence, construct prompts or assign scores."""

import contextlib
import hashlib
import os
from pathlib import Path

from .models import Evaluation, Question, Recipe
from .storage import RunIntegrityError, digest, read_json


@contextlib.contextmanager
def exclusive_run(output: Path):
    # The ECS runner targets POSIX; refusing unsupported locking is safer than
    # silently claiming a cross-process call cap on an unsupported platform.
    import fcntl

    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / ".run.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunIntegrityError("Another process owns this run directory") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def environment_identity(recipe: Recipe) -> dict:
    models = {}
    for role in ("reader", "judge", "rewriter"):
        settings = getattr(recipe, role)
        if settings is not None:
            # Hash endpoints as well: URLs can contain credentials/query strings.
            models[role] = digest(os.environ[settings.base_url_env])
    if recipe.environment_report is None:
        raise RunIntegrityError("A current service configuration report is required")
    return {
        "model_endpoints_sha256": models,
        "source_connection_sha256": digest(os.environ[recipe.postgres_dsn_env])
        if recipe.source == "postgres"
        else None,
        "service_configuration_sha256": hashlib.sha256(
            recipe.environment_report.read_bytes()
        ).hexdigest(),
    }


def validate_evaluations(recipe: Recipe, questions: list[Question]) -> None:
    # Validation only; do not pass evaluation objects into retrieval or Reader.
    data = read_json(recipe.evaluation)
    for question in questions:
        value = Evaluation.model_validate(data[question.id])
        if recipe.dataset == "beam10m" and (
            not value.rubric
            and not value.answer.strip()
            or value.rubric
            and any(not item.strip() for item in value.rubric)
        ):
            raise RunIntegrityError("BEAM evaluation has no usable scoring evidence")
