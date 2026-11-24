"""#119: retain baseline, add two query-only recalls, interleave whole documents."""

import itertools

import tiktoken

from .models import EvidenceDocument, Recall

REWRITE_PREFIX = "Rewrite the following memory search question into exactly two complementary search queries for retrieving its supporting conversation evidence. Preserve explicit names, dates, constraints and negations; do not guess the answer or invent events. Split distinct aspects when helpful. Each query must be self-contained, under 1200 characters. In the answer field return only a JSON array of exactly two strings; put any explanation in reasoning. Only the search question is available.\n\nSEARCH QUESTION:\n"


def documents(recall: Recall) -> list[EvidenceDocument]:
    """Pinned AMB formatting: memory identity dedup, chunk text inlined once per recall."""
    output = []
    seen_facts: set[str] = set()
    seen_chunks: set[str] = set()
    for memory in recall.results:
        if memory.id in seen_facts:
            continue
        seen_facts.add(memory.id)
        lines = [f"**[{memory.type}]** {memory.text}" if memory.type else memory.text]
        meta = []
        if (
            memory.occurred_start
            and memory.occurred_end
            and memory.occurred_start != memory.occurred_end
        ):
            meta.append(f"occurred: {memory.occurred_start} – {memory.occurred_end}")
        elif memory.occurred_start:
            meta.append(f"occurred: {memory.occurred_start}")
        if memory.mentioned_at:
            meta.append(f"mentioned: {memory.mentioned_at}")
        if memory.chunk_id:
            meta.append(f"chunk: {memory.chunk_id}")
        if meta:
            lines.append("_" + " · ".join(meta) + "_")
        if (
            memory.chunk_id
            and memory.chunk_id not in seen_chunks
            and memory.chunk_id in (recall.chunks or {})
        ):
            lines.append("> " + str(recall.chunks[memory.chunk_id]["text"]))
            seen_chunks.add(memory.chunk_id)
        output.append(EvidenceDocument(id=memory.id, content="\n".join(lines)))
    return output


def render(docs: list[EvidenceDocument]) -> str:
    return "\n\n".join(
        f"## Memory {index + 1}\n{doc.content}" for index, doc in enumerate(docs)
    )


def merge(
    baseline: Recall, extra: list[Recall], budget: int = 60000
) -> list[EvidenceDocument]:
    if len(extra) != 2:
        raise ValueError(
            "The historical BEAM recipe requires exactly two supplementary recalls"
        )
    encoder = tiktoken.get_encoding("cl100k_base")
    selected = documents(baseline)
    used = sum(
        len(encoder.encode(f"## Memory {i + 1}\n{d.content}\n\n"))
        for i, d in enumerate(selected)
    )
    if used > budget:
        raise ValueError("Baseline exceeds budget; cannot silently truncate baseline")
    seen = {d.id for d in selected}
    for group in itertools.zip_longest(*(documents(r) for r in extra)):
        for doc in group:
            if doc is None or doc.id in seen:
                continue
            seen.add(doc.id)
            size = len(
                encoder.encode(f"## Memory {len(selected) + 1}\n{doc.content}\n\n")
            )
            if used + size <= budget:
                used += size
                selected.append(doc)
    return selected


def validate_queries(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Expected exactly two complementary queries")
    if not all(isinstance(x, str) and 0 < len(x.strip()) <= 1200 for x in value):
        raise ValueError("Invalid query text")
    return value
