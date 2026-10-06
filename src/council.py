"""AI Council: several models answer, review each other blind, a chairman decides.

Stage 1 (opinions)  - every member answers the question independently.
Stage 2 (review)    - every member sees the answers under shuffled letters
                      (Response A, B, ...), critiques them and ends with a
                      FINAL RANKING. Votes on a reviewer's own answer are
                      dropped before the rankings are averaged.
Stage 3 (synthesis) - the chairman reads the answers, the reviews and the
                      aggregate ranking and writes the council's answer.

Members can be any Odysseus model endpoint: a Claude or ChatGPT subscription
or an API/local endpoint. Each call goes through ``llm_core.stream_llm`` so the
provider differences live in one place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import string
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MODE_FULL = "full"
MODE_QUICK = "quick"
MODES = (MODE_FULL, MODE_QUICK)

MAX_MEMBERS = 8
MAX_QUESTION_CHARS = 20000
# Bounds on what is replayed into later stages, so the chairman prompt stays
# inside small context windows even with eight long answers.
MAX_ANSWER_CHARS = 12000
MAX_REVIEW_CHARS = 6000
HISTORY_TURNS = 3
HISTORY_CHARS = 4000

MEMBER_TIMEOUT = 600
CHAIR_TIMEOUT = 900

OPINION_SYSTEM = (
    "You are one member of an AI council. Several independent models answer the same "
    "question; your answer will be peer reviewed and then synthesized by a chairman.\n"
    "Answer the user's question directly and completely. Be accurate and specific, show "
    "the reasoning that matters, and say plainly where you are uncertain or where the "
    "evidence is weak. No preamble and no filler."
)

REVIEW_SYSTEM = (
    "You are a rigorous reviewer on an AI council. You will see several anonymous answers "
    "to the same question. Judge them on correctness first, then on completeness, insight "
    "and clarity. Point out concrete errors and omissions; do not reward length or confidence."
)

CHAIR_SYSTEM = (
    "You are the chairman of an AI council. Several models answered the user's question "
    "and reviewed each other's answers. Write the single best final answer for the user.\n"
    "- Build on the strongest material; correct anything the reviews showed to be wrong.\n"
    "- Where members genuinely disagree on something that matters, say so and state which "
    "view you adopt and why.\n"
    "- Address the user directly. Start with the answer itself: no title, no heading "
    "like 'Final Answer', no preamble. Do not narrate the council process or refer to "
    "'Response A' etc. unless a disagreement needs it."
)


@dataclass
class Member:
    """A resolved council seat: where to send the call and how to show it."""

    endpoint_id: str
    model: str
    endpoint_name: str
    kind: str          # subscription | api | local
    provider: str
    url: str = field(repr=False, default="")
    headers: Optional[Dict[str, str]] = field(repr=False, default=None)
    billing: str = "metered"                 # metered | subscription | local
    input_per_mtok: Optional[float] = None
    output_per_mtok: Optional[float] = None

    def public(self, index: Any) -> Dict[str, Any]:
        return {
            "index": index,
            "endpoint_id": self.endpoint_id,
            "model": self.model,
            "endpoint_name": self.endpoint_name,
            "kind": self.kind,
            "provider": self.provider,
            "billing": self.billing,
        }

    def cost_usd(self, usage: Optional[Dict[str, int]]) -> Optional[float]:
        """Metered cost of one call, or None when free, flat-rate or unpriced."""
        if not usage or self.billing != "metered" or self.input_per_mtok is None:
            return None
        out_price = self.output_per_mtok if self.output_per_mtok is not None else self.input_per_mtok
        return round((usage.get("input_tokens", 0) * self.input_per_mtok
                      + usage.get("output_tokens", 0) * out_price) / 1_000_000, 6)


class CouncilError(RuntimeError):
    """User-facing council configuration error."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

_RANKING_HEADER_RE = re.compile(r"final\s+ranking\s*:?", re.IGNORECASE)
_RESPONSE_LABEL_RE = re.compile(r"Response\s+([A-Z])\b")


def letters(n: int) -> List[str]:
    return list(string.ascii_uppercase[:n])


def assign_labels(member_indexes: List[int], rng: Optional[random.Random] = None) -> Dict[str, int]:
    """Map shuffled letters to member indexes so answer order reveals nothing."""
    order = list(member_indexes)
    (rng or random).shuffle(order)
    return {label: idx for label, idx in zip(letters(len(order)), order)}


def parse_ranking(text: str, valid_labels: List[str]) -> List[str]:
    """Labels in ranked order from a review's FINAL RANKING section.

    A review without that section counts as unranked: the order a reviewer
    happens to mention answers in is not a vote, and averaging it in would
    present noise as a blind ranking. Unknown and repeated labels are ignored.
    """
    if not text:
        return []
    valid = set(valid_labels)
    matches = list(_RANKING_HEADER_RE.finditer(text))
    if not matches:
        return []
    section = text[matches[-1].end():]
    ranked: List[str] = []
    for label in _RESPONSE_LABEL_RE.findall(section):
        if label in valid and label not in ranked:
            ranked.append(label)
    return ranked


def aggregate_rankings(
    reviews: List[Dict[str, Any]],
    labels: Dict[str, int],
) -> List[Dict[str, Any]]:
    """Average rank per answer, ignoring each reviewer's vote on its own answer.

    ``reviews`` items carry ``member`` (the reviewer's index) and ``ranking``
    (a list of labels, best first). Positions are re-counted after removing the
    reviewer's own label so self-votes cannot shift anyone's place.
    """
    positions: Dict[str, List[int]] = {label: [] for label in labels}
    first_votes: Dict[str, int] = {label: 0 for label in labels}
    for review in reviews:
        ranking = review.get("ranking") or []
        reviewer = review.get("member")
        filtered = [label for label in ranking if label in labels and labels[label] != reviewer]
        for pos, label in enumerate(filtered, start=1):
            positions[label].append(pos)
        if filtered:
            first_votes[filtered[0]] += 1
    rows = []
    for label, member in labels.items():
        votes = positions[label]
        rows.append({
            "label": label,
            "member": member,
            "avg_rank": round(sum(votes) / len(votes), 2) if votes else None,
            "votes": len(votes),
            "first_votes": first_votes[label],
        })
    rows.sort(key=lambda r: (r["avg_rank"] is None, r["avg_rank"] or 0, -r["first_votes"], r["label"]))
    return rows


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[... truncated ...]"


def history_messages(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Earlier council turns as chat history (question -> council answer)."""
    msgs: List[Dict[str, str]] = []
    for turn in history[-HISTORY_TURNS:]:
        q = (turn.get("question") or "").strip()
        a = (turn.get("final") or "").strip()
        if q and a:
            msgs.append({"role": "user", "content": _clip(q, HISTORY_CHARS)})
            msgs.append({"role": "assistant", "content": _clip(a, HISTORY_CHARS)})
    return msgs


def opinion_messages(question: str, history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [{"role": "system", "content": OPINION_SYSTEM}, *history_messages(history),
            {"role": "user", "content": question}]


def _answers_block(labels: Dict[str, int], opinions: Dict[int, str], names: Optional[Dict[int, str]] = None) -> str:
    parts = []
    for label, member in labels.items():
        title = f"Response {label}"
        if names:
            title += f" ({names[member]})"
        parts.append(f"### {title}\n{_clip(opinions.get(member, ''), MAX_ANSWER_CHARS)}")
    return "\n\n".join(parts)


def review_messages(question: str, labels: Dict[str, int], opinions: Dict[int, str]) -> List[Dict[str, str]]:
    label_list = ", ".join(f"Response {label}" for label in labels)
    example = "\n".join(f"{i}. Response {label}" for i, label in enumerate(labels, start=1))
    user = (
        f"Question:\n{question}\n\n"
        f"Anonymous answers ({label_list}):\n\n{_answers_block(labels, opinions)}\n\n"
        "Review each answer in turn: what it gets right, what it gets wrong or misses. "
        "Be brief and concrete.\n\n"
        "Then end with a line that says exactly FINAL RANKING: followed by a numbered list "
        "of every response from best to worst, one per line, and nothing after it. Format:\n"
        f"FINAL RANKING:\n{example}"
    )
    return [{"role": "system", "content": REVIEW_SYSTEM}, {"role": "user", "content": user}]


def chair_messages(
    question: str,
    history: List[Dict[str, str]],
    labels: Dict[str, int],
    opinions: Dict[int, str],
    names: Dict[int, str],
    reviews: List[Dict[str, Any]],
    ranking: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    sections = [f"Question:\n{question}", f"Council answers:\n\n{_answers_block(labels, opinions, names)}"]
    member_label = {member: label for label, member in labels.items()}
    review_parts = []
    for review in reviews:
        text = (review.get("text") or "").strip()
        if text:
            who = member_label.get(review.get("member"), "?")
            review_parts.append(f"### Review by the author of Response {who}\n{_clip(text, MAX_REVIEW_CHARS)}")
    if review_parts:
        sections.append("Peer reviews:\n\n" + "\n\n".join(review_parts))
    ranked = [r for r in ranking if r.get("avg_rank") is not None]
    if ranked:
        lines = [f"{i}. Response {r['label']} - average rank {r['avg_rank']} over {r['votes']} review(s)"
                 for i, r in enumerate(ranked, start=1)]
        sections.append("Aggregate peer ranking (self-votes excluded):\n" + "\n".join(lines))
    sections.append("Write the council's final answer to the question now.")
    return [{"role": "system", "content": CHAIR_SYSTEM}, *history_messages(history),
            {"role": "user", "content": "\n\n".join(sections)}]


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def iter_sse(chunk: str):
    """(event, payload) pairs from one stream_llm chunk; payload None for [DONE]."""
    event = ""
    for line in str(chunk).splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        if raw == "[DONE]":
            yield "done", None
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        yield event or "data", payload
        event = ""


def _error_text(payload: Any) -> str:
    if isinstance(payload, dict):
        text = payload.get("text") or payload.get("error") or payload.get("detail")
        if isinstance(text, str) and text.strip():
            return text.strip()[:400]
    return "The model request failed."


Emit = Callable[[Dict[str, Any]], Awaitable[None]]
StreamFn = Callable[..., AsyncIterator[str]]


async def run_member(
    stream_fn: StreamFn,
    member: Member,
    messages: List[Dict[str, str]],
    *,
    stage: str,
    index: Any,
    emit: Emit,
    timeout: int,
    sink: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Stream one member's reply, emitting deltas; returns {text, error, ms, usage, cost_usd}.

    ``sink`` (when given) is updated in place as text arrives and when the call
    ends, so a run that is stopped midway still has what was written.
    """
    started = time.monotonic()
    parts: List[str] = []
    thinking_sent = False
    error: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    if sink is not None:
        sink.update({"text": "", "error": None, "ms": None, "pending": True})
    try:
        async for chunk in stream_fn(member.url, member.model, messages, headers=member.headers,
                                     timeout=timeout, workload="foreground"):
            for event, payload in iter_sse(chunk):
                if event == "error":
                    error = _error_text(payload)
                    break
                if payload is None or not isinstance(payload, dict):
                    continue
                if payload.get("type") == "usage" and isinstance(payload.get("data"), dict):
                    data = payload["data"]
                    try:
                        usage = {"input_tokens": int(data.get("input_tokens") or 0),
                                 "output_tokens": int(data.get("output_tokens") or 0)}
                    except (TypeError, ValueError):
                        usage = None
                    continue
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    if payload.get("thinking"):
                        if not thinking_sent:
                            thinking_sent = True
                            await emit({"type": "thinking", "stage": stage, "member": index})
                        continue
                    parts.append(delta)
                    if sink is not None:
                        sink["text"] = sink.get("text", "") + delta
                    await emit({"type": "delta", "stage": stage, "member": index, "text": delta})
            if error:
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("council member call failed stage=%s error_type=%s", stage, type(exc).__name__)
        error = "The model request failed."
    text = "".join(parts).strip()
    try:
        from src.text_helpers import strip_think

        text = (strip_think(text) or text).strip()
    except Exception:
        pass
    if not error and not text:
        error = "The model returned an empty answer."
    ms = int((time.monotonic() - started) * 1000)
    cost = member.cost_usd(usage)
    outcome = {"text": text, "error": error, "ms": ms, "usage": usage, "cost_usd": cost}
    if sink is not None:
        sink.update(outcome)
        sink.pop("pending", None)
    if error:
        await emit({"type": "member_error", "stage": stage, "member": index, "error": error, "ms": ms,
                    "usage": usage, "cost_usd": cost})
    else:
        await emit({"type": "member_done", "stage": stage, "member": index, "ms": ms,
                    "usage": usage, "cost_usd": cost})
    return outcome


async def run_parallel(jobs: List[Awaitable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return list(await asyncio.gather(*jobs))


async def run_council(
    *,
    question: str,
    members: List[Member],
    chairman: Member,
    mode: str,
    history: List[Dict[str, str]],
    emit: Emit,
    stream_fn: Optional[StreamFn] = None,
    rng: Optional[random.Random] = None,
    checkpoint: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run all stages, emitting progress events; returns the turn's results.

    Never raises for a model failure: failed members are recorded and left out
    of later stages. The result's ``error`` is set only when nothing usable
    came back.

    ``state`` (when given) is the result dict, filled in place as each member
    streams and finishes, so a caller that stops the run still holds every
    answer written so far, including a half-written synthesis.
    ``checkpoint`` receives it after each completed stage.
    """
    result: Dict[str, Any] = state if state is not None else {}
    result.update({"opinions": [], "reviews": [], "ranking": [], "labels": {},
                   "final": "", "error": None})

    async def _checkpoint() -> None:
        if checkpoint is not None:
            try:
                await checkpoint(result)
            except Exception as exc:
                logger.warning("council checkpoint failed error_type=%s", type(exc).__name__)

    if stream_fn is None:
        from src.llm_core import stream_llm as stream_fn  # noqa: N806

    # Stage 1 - opinions
    await emit({"type": "stage", "stage": "opinions", "status": "start"})
    base = opinion_messages(question, history)
    result["opinions"] = [{"member": i} for i in range(len(members))]
    outcomes = await run_parallel([
        run_member(stream_fn, m, base, stage="opinions", index=i, emit=emit, timeout=MEMBER_TIMEOUT,
                   sink=result["opinions"][i])
        for i, m in enumerate(members)
    ])
    await emit({"type": "stage", "stage": "opinions", "status": "done"})
    ok = {i: o["text"] for i, o in enumerate(outcomes) if not o["error"]}
    if not ok:
        result["error"] = "No council member produced an answer."
        return result

    labels = assign_labels(sorted(ok), rng)
    result["labels"] = labels
    await _checkpoint()
    names = {i: f"{members[i].model} via {members[i].endpoint_name}" for i in ok}

    # Stage 2 - blind peer review
    if mode == MODE_FULL and len(ok) >= 2:
        await emit({"type": "stage", "stage": "review", "status": "start", "labels": labels})
        msgs = review_messages(question, labels, ok)
        reviewer_ids = sorted(ok)
        result["reviews"] = [{"member": i} for i in reviewer_ids]
        await run_parallel([
            run_member(stream_fn, members[i], msgs, stage="review", index=i, emit=emit, timeout=MEMBER_TIMEOUT,
                       sink=sink)
            for i, sink in zip(reviewer_ids, result["reviews"])
        ])
        valid = list(labels)
        for review in result["reviews"]:
            review["ranking"] = [] if review.get("error") else parse_ranking(review.get("text", ""), valid)
        result["ranking"] = aggregate_rankings([r for r in result["reviews"] if not r.get("error")], labels)
        await emit({"type": "ranking", "ranking": result["ranking"], "labels": labels})
        await emit({"type": "stage", "stage": "review", "status": "done"})
        await _checkpoint()

    # Stage 3 - chairman synthesis
    await emit({"type": "stage", "stage": "synthesis", "status": "start"})
    msgs = chair_messages(question, history, labels, ok, names,
                          [r for r in result["reviews"] if not r.get("error")], result["ranking"])
    result["chair"] = {"member": "chair"}
    chair = await run_member(stream_fn, chairman, msgs, stage="synthesis", index="chair",
                             emit=emit, timeout=CHAIR_TIMEOUT, sink=result["chair"])
    await emit({"type": "stage", "stage": "synthesis", "status": "done"})
    if chair["error"]:
        result["error"] = f"Chairman failed: {chair['error']}"
        # Fall back to the best-ranked answer so the turn still has a result.
        best = next((r["member"] for r in result["ranking"] if r.get("avg_rank") is not None), None)
        if best is None:
            best = sorted(ok)[0]
        result["final"] = ok[best]
        result["final_fallback_member"] = best
    else:
        result["final"] = chair["text"]
    result["chair_ms"] = chair["ms"]
    return result


def usage_totals(result: Dict[str, Any]) -> Dict[str, Any]:
    """Tokens and metered cost across every call of a turn."""
    calls = [*(result.get("opinions") or []), *(result.get("reviews") or [])]
    if isinstance(result.get("chair"), dict):
        calls.append(result["chair"])
    tokens_in = tokens_out = 0
    cost = 0.0
    priced = unpriced = 0
    for call in calls:
        usage = call.get("usage") or {}
        tokens_in += int(usage.get("input_tokens") or 0)
        tokens_out += int(usage.get("output_tokens") or 0)
        if call.get("cost_usd") is not None:
            cost += float(call["cost_usd"])
            priced += 1
        elif usage:
            unpriced += 1
    return {"input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round(cost, 6) if priced else None, "priced_calls": priced,
            "unpriced_calls": unpriced}
