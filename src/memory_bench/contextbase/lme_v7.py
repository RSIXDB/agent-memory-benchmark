"""Historical #115 v7 algorithm, isolated from DB and experiment directories.

Source SHA256: d3b4233b63215faea3bab09c1ecdf39d5e089e15d93ad26af0dfae8484fca923
The explicit view filter and empty secondary section are preserved for replay fidelity.
"""

import collections
import hashlib
import json
import math
import re
import tiktoken

ENC = tiktoken.get_encoding("o200k_base")
STOP = set(
    "a an the i me my we our you your is are was were be been have has had do does did of to in on at by for from and or but this that it its as with what which who whom when where how many much can could would should about please tell".split()
)
SEX = re.compile(
    "\\b(crotch|pubic|genital\\w*|erection|penetrat\\w*|naked|nude|intercourse|molest\\w*)\\b",
    re.I,
)
QUESTION_PREFIX = re.compile(
    "^(who|what|when|where|why|how|can|could|would|should|is|are|do|does|did|tell|give|explain|list|write|recommend)\\b",
    re.I,
)
SHORT_ANSWER = re.compile(
    "^(yes|no|yeah|yep|sure|exactly|it\\b|that\\b|this\\b|those\\b|these\\b|sounds\\b|maybe\\b|not\\b|about\\b|around\\b|since\\b|for\\b|only\\b|usually\\b|probably\\b|well\\b|[0-9])",
    re.I,
)
MINOR = re.compile(
    "\\b(underage|minor|teen\\w*|school|child\\w*|[1-9]-year-old|1[0-7]-year-old)\\b",
    re.I,
)


def words(s):
    return [
        w for w in re.findall("[a-z0-9]+(?:[-'][a-z0-9]+)*", s.lower()) if w not in STOP
    ]


def tokens(s):
    return len(ENC.encode(s, disallowed_special=()))


def clipped(s, n, tail=False):
    t = ENC.encode(s, disallowed_special=())
    return ENC.decode(t[-n:] if tail else t[:n]) if len(t) > n else s


def bm25(texts, query):
    seqs = [words(t) for t in texts]
    n = len(seqs) or 1
    avg = sum(map(len, seqs)) / n or 1
    df = collections.Counter((w for seq in seqs for w in set(seq)))
    query = set(words(query))
    scores = []
    for seq in seqs:
        tf = collections.Counter(seq)
        norm = 1.2 * (0.25 + 0.75 * len(seq) / avg)
        scores.append(
            sum(
                (
                    math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                    * tf[w]
                    * 2.2
                    / (tf[w] + norm)
                    for w in query
                    if tf[w]
                )
            )
        )
    return scores


def assemble(query, rows, response, budget):
    _query_wrapper = {"question": query}
    docs = []
    blocked = []
    source_hashes = {}
    for row in rows:
        text = row["original_text"]
        source_hashes[row["id"]] = hashlib.sha256(text.encode()).hexdigest()
        if SEX.search(text) and MINOR.search(text):
            blocked.append(
                {
                    "document_id": row["id"],
                    "source_sha256": source_hashes[row["id"]],
                    "reason": "Conservative sexual/minor-or-school term cooccurrence; no source text copied",
                }
            )
            continue
        messages = json.loads(text)
        params = row["retain_params"]
        params = json.loads(params) if isinstance(params, str) else params
        docs.append(
            {"id": row["id"], "date": params["event_date"], "messages": messages}
        )
    docs.sort(key=lambda d: (d["date"], d["id"]))
    user = []
    assist = []
    native_rank = {}
    for i, m in enumerate(response.get("results", [])[:40]):
        if m.get("document_id"):
            native_rank.setdefault(m["document_id"], i)
    for doc in docs:
        for i, m in enumerate(doc["messages"]):
            content = m.get("content", "")
            assert isinstance(content, str)
            label = f"Session {doc['id']} | {doc['date']} | turn {i}"
            if m["role"] == "user":
                text = "USER " + str(i) + ": " + content
                if (
                    i
                    and len(content) < 160
                    and (doc["messages"][i - 1]["role"] == "assistant")
                    and (
                        SHORT_ANSWER.search(content.strip())
                        or (
                            len(content.split()) <= 7
                            and (not QUESTION_PREFIX.search(content.strip()))
                        )
                    )
                ):
                    text += (
                        "\nPRECEDING ASSISTANT (context for the short reply): "
                        + clipped(doc["messages"][i - 1]["content"], 320, tail=True)
                    )
                user.append(
                    {
                        "key": (doc["date"], doc["id"], i),
                        "id": doc["id"] + ":" + str(i),
                        "text": text,
                        "score_text": content,
                        "header": "Session " + doc["id"] + " | " + doc["date"],
                    }
                )
            elif m["role"] == "assistant":
                prev = (
                    doc["messages"][i - 1]["content"]
                    if i and doc["messages"][i - 1]["role"] == "user"
                    else ""
                )
                text = label + "\n"
                if prev:
                    text += "PRECEDING USER: " + clipped(prev, 400) + "\n"
                text += "ASSISTANT: " + content
                assist.append(
                    {
                        "id": doc["id"] + ":" + str(i),
                        "document_id": doc["id"],
                        "text": text,
                        "score_text": prev + "\n" + content,
                    }
                )
    assert user
    all_user_count = len(user)
    assert user
    user_budget = min(30000, int(budget * 0.63))
    assistant_budget = min(16000, int(budget * 0.34))
    fact_budget = 0
    parts = []
    user_used = 0
    included_users = []
    sizes = [tokens(x["text"]) + 2 for x in user]
    order = list(range(len(user)))
    full_header_cost = sum((tokens(v) + 2 for v in {x["header"] for x in user}))
    if sum(sizes) + full_header_cost > user_budget:
        scores = bm25([x["score_text"] for x in user], _query_wrapper["question"])
        order = sorted(order, key=lambda i: (-scores[i], user[i]["key"]))
    selected = []
    seen_headers = set()
    for i in order:
        extra_header = (
            0 if user[i]["header"] in seen_headers else tokens(user[i]["header"]) + 2
        )
        if user_used + sizes[i] + extra_header <= user_budget:
            selected.append(i)
            user_used += sizes[i] + extra_header
            seen_headers.add(user[i]["header"])
    last_header = None
    for i in sorted(selected, key=lambda i: user[i]["key"]):
        if user[i]["header"] != last_header:
            parts.append(user[i]["header"])
            last_header = user[i]["header"]
        parts.append(user[i]["text"])
        included_users.append(user[i]["id"])
    user_section = (
        "PRIMARY SOURCE: USER UTTERANCES IN CHRONOLOGICAL SESSION ORDER\nThese are original user utterances, not extracted autobiographical assertions. Requests, quoted examples and role-play remain requests/examples. Short replies include their preceding assistant context.\n\n"
        + "\n\n".join(parts)
    )
    scores = bm25([x["score_text"] for x in assist], _query_wrapper["question"])
    ranked = sorted(
        range(len(assist)),
        key=lambda i: (
            -(
                scores[i]
                + (
                    0.8 / (1 + native_rank[assist[i]["document_id"]])
                    if assist[i]["document_id"] in native_rank
                    else 0
                )
            ),
            assist[i]["id"],
        ),
    )
    assistant_parts = []
    assistant_used = 0
    included_assist = []
    for i in ranked:
        item = assist[i]
        n = tokens(item["text"]) + 2
        if assistant_used + n <= assistant_budget:
            assistant_parts.append(item["text"])
            assistant_used += n
            included_assist.append(item["id"])
    fact_parts = []
    facts_used = 0
    blocked_ids = {x["document_id"] for x in blocked}
    for m in response.get("results", []):
        if m.get("document_id") in blocked_ids:
            continue
        text = "MEMORY LEAD: " + m["text"]
        dates = [
            str(m[k])
            for k in ["occurred_start", "occurred_end", "mentioned_at"]
            if m.get(k)
        ]
        if dates:
            text += " | dates: " + "; ".join(dates)
        if m.get("document_id"):
            text += " | source: " + m["document_id"]
        n = tokens(text) + 2
        if facts_used + n <= fact_budget:
            fact_parts.append(text)
            facts_used += n
    sections = [
        user_section,
        "PRIMARY SOURCE: QUERY-RELEVANT ASSISTANT WINDOWS\nAssistant replies establish recommendations and explanations; they do not by themselves prove the user took an action.\n\n"
        + "\n\n".join(assistant_parts),
        "SECONDARY NATIVE SEMANTIC MEMORY LEADS\nUse the original speaker/source wording above when a summary is inconsistent.\n\n"
        + "\n\n".join(fact_parts),
    ]
    text = "\n\n".join(sections)
    assert tokens(text) <= budget, (tokens(text), budget)
    return {
        "text": text,
        "tokens": tokens(text),
        "budget": budget,
        "method": "session-grouped-source-v7-user30k-assistant16k-no-memory-leads",
        "source": "Native database documents.original_text, after declared corpus filter; no gold or evidence IDs",
        "all_user_utterances_included": len(included_users) == all_user_count,
        "all_eligible_user_utterances_included": len(included_users) == len(user),
        "nonpersonal_irrelevant_user_turns_omitted": all_user_count - len(user),
        "all_source_user_turn_count": all_user_count,
        "user_utterances_included": len(included_users),
        "user_utterances_available": len(user),
        "assistant_windows_included": len(included_assist),
        "assistant_windows_available": len(assist),
        "native_memory_leads": len(fact_parts),
        "returned_facts": len(response.get("results", [])),
        "returned_chunks": len(response.get("chunks") or {}),
        "blocks_included": len(included_users) + len(included_assist) + len(fact_parts),
        "blocks_available": len(user) + len(assist) + len(response.get("results", [])),
        "user_token_budget": user_budget,
        "assistant_token_budget": assistant_budget,
        "memory_lead_token_budget": fact_budget,
        "source_document_count": len(rows),
        "source_document_hashes": source_hashes,
        "view_safety_exclusions": blocked,
        "view_safety_filter": "Whole-session omission on conservative explicit-sexual and minor/school term cooccurrence; may omit false positives",
        "user_turn_ids": included_users,
        "assistant_turn_ids": included_assist,
        "native_semantic_prior": "Top40 returned native facts supply document priors for assistant window ranking; no original-corpus/gold scan",
    }
