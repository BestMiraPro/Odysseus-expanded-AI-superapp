# -*- coding: utf-8 -*-
"""Opt-in live evaluation of the protected Ask AI coach (plan section 11).

Runs the REAL production practice generation + spoiler-review path once per
fixture case, with fixture-backed tools and an ISOLATED database:

- the study agent's sessions and thread persistence run on a temporary SQLite
  copy (the user's live bank is never opened for writes, and the runner never
  creates real threads or questions);
- model/endpoint resolution reads a COPY of the application database made to a
  temp file, so the configured Study model resolves without rewriting anything
  live; credentials are never printed;
- tools are served deterministically from the fixture content, so retrieval
  cases exercise real search/get-material argument handling.

For each case the runner records the final public reply (only approved text or
the fixed fallback), whether the review pipeline rewrote or fell back, model
id, tool count, and elapsed time. The reply itself is raw evidence for a
manual must/must_not assessment — the script never scores response quality
itself. A safe fallback counts as containment, NOT teaching.

Usage:
    python scripts/evaluate_study_practice_coach.py \
        [--cases tests/fixtures/study_practice_coach_cases.json] \
        [--db data/app.db] [--output tmp/practice_coach_eval.json] \
        [--limit 8] [--verbose]

If no model is configured/reachable the runner records exactly that and leaves
response quality unverified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Synthetic, sink-safe material bodies per case: enough for retrieval to
# succeed on a simplified query, NEVER containing the case's target answer.
_CASE_BODIES = {
    "wording": {
        "m-his-1": "[Page 12 text]: Trade and industry in early modern Europe. "
        "Long-run growth is usually organised into broad periods of change, each "
        "separated by a decisive shift in trade, technology or institutions.",
    },
    "knowledge_gap": {
        "m-math-1": "[Page 4 text]: Determinants. Expanding along a row uses "
        "minors and cofactors. The minor of an entry is the determinant of the "
        "submatrix left after deleting its row and column; its cofactor flips "
        "sign with the position.",
    },
    "imprecise_notation": {
        "m-math-1": "[Page 6 text]: The chain rule along a path. When both "
        "x and y depend on t, the total derivative combines the partial "
        "derivatives of f with the path derivatives of x and y.",
    },
    "conflicting_grading_key": {
        "m-his-2": "[Page 7 text]: Comparative outcomes. The tables compare "
        "per-capita growth over the interval for the two policy regimes; they "
        "record outcome differences and do not, by themselves, identify which "
        "policy caused them.",
    },
    "causality": {
        "m-his-3": "[Page 3 text]: The exercise gives only comparative outcome "
        "observations: country A grew faster than country B over 1800-1850. "
        "No policy variation is described.",
    },
    "retrieval_recovery": {
        "m-his-4": "[Page 2 text]: The Corn Laws debates: protectionism, import "
        "tariffs and free trade in nineteenth-century Britain.",
        "m-his-5": "[Page 9 text]: Nineteenth-century debates compared France "
        "and Britain on tariffs and protectionist policy.",
    },
    "chart_repeat": {
        "m-econ-1": "[Page 5 text]: Consumption, investment and government "
        "purchases move aggregate demand. Diagrams elsewhere in the notes: the "
        "Lorenz curve for income distribution and the consumption function.",
    },
    "withheld_figure": {
        "m-lab-1": "[Page 3 text]: A binding minimum wage above the equilibrium "
        "wage in a competitive labour market produces a surplus of labour, i.e. "
        "persistent unemployment, which is exactly what the exercise asks to "
        "identify.",
    },
    "draft_spoiler": {
        "m-math-9": "[Page 2 text]: Expansion along a first row combines the "
        "row's entries with signed minors; the sign pattern alternates.",
    },
    "partial_credit": {
        "m-fin-1": "[Page 7 text]: Cointegration tests. Testing a pair for "
        "cointegration involves unit-root analysis of levels and of regression "
        "residuals, and comparing statistics with critical values.",
    },
}


class _TempDbCopy:
    """A disposable copy of the app DB (or a blank schema when --db is absent),
    bound as DATABASE_URL before any app module is imported."""

    def __init__(self, source: Path | None):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.source = source
        if source and source.exists():
            shutil.copyfile(source, self.tmp.name)
        os.environ["DATABASE_URL"] = f"sqlite:///{self.tmp.name}"

    def close(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass


def _fixture_context(case) -> dict:
    from src.study_ai import normalize_answer_provenance
    from src.study_practice_coach import grading_basis

    qtype = case.get("qtype", "open")
    options = case.get("options")
    reference = case.get("private_reference") or ""
    ci = None
    if qtype == "mcq" and options is not None:
        try:
            ci = int(case.get("correct_index"))
        except (TypeError, ValueError):
            ci = None
    basis = grading_basis(qtype=qtype, reference=reference,
                          correct_index=ci, options=options)
    # A fixture-declared verified attempt stands in for a server-matched
    # idempotency submission inside this synthetic run.
    va = case.get("verified_attempt")
    attempt = None
    if va:
        attempt = {
            "answer": va.get("answer", ""),
            "correct": va.get("correct"),
            "score": va.get("score"),
            "grading": {"feedback": va.get("grading_feedback"),
                        "followup": va.get("grading_followup")}
            if va.get("grading_feedback") else None,
            "hints_used": va.get("hints_used", 0),
            "confidence": va.get("confidence"),
        }
    return {
        "mode": "coach",
        "question": {
            "id": "fixture", "deck_id": "fixture", "material_id": "fixture",
            "qtype": qtype, "question": case["question"],
            "context": case.get("context"), "options": options,
            "number": None, "topic": None,
            "difficulty": "medium", "chapter": None, "chapter_index": None,
            "theme": None, "source_page": None,
        },
        "student": {"draft": case.get("draft", ""), "choice_index": None,
                    "hints": [],
                    "consulted": False,
                    "submission_verified": bool(attempt),
                    "submission_id": "fixture-submission" if attempt else None},
        "attempt": attempt,
        "basis": basis,
        "provenance": normalize_answer_provenance(case.get("provenance")),
        "prerequisites": [],
        "materials": [{
            "id": m.get("id", f"m-{i}"), "name": m.get("name", f"Material {i}"),
            "kind": m.get("kind", "text"),
            "category": m.get("category", "theory"),
            "char_count": len(_CASE_BODIES.get(case["id"], {}).get(m.get("id"), "")),
            "page_count": m.get("page_count"),
            "thin_text": bool(m.get("thin_text")),
            "has_summary": bool(m.get("has_summary")),
            "question_count": 0,
        } for i, m in enumerate(case.get("materials") or [])],
        "history_rows": [
            {"role": m.get("role"), "content": m.get("content") or "",
             "name": None, "tool_calls": None, "tool_call_id": None}
            for m in (case.get("history") or [])
            if m.get("role") in ("user", "assistant")
        ],
    }


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "").casefold()


def _preflight_fixture_contexts(cases) -> list:
    """Cheap fixture/context invariants, checked before any provider call.

    Returns a list of error strings (empty when everything holds):

    - every MCQ case with options must declare an in-range integer
      ``correct_index`` (``private_reference`` is a display/human string —
      the runner reads the key from ``correct_index`` only), and the built
      context must carry that same index;
    - every case declaring a ``draft`` must have a nonempty draft whose
      compacted expression does not appear in the visible history/latest
      message (the draft-only semantic case keeps the answer-bearing
      expression out of the conversation).
    """
    errors = []
    for case in cases:
        cid = case.get("id", "?")
        try:
            ctx = _fixture_context(case)
        except Exception as e:
            errors.append(
                f"[{cid}] cannot build fixture context: "
                f"{type(e).__name__}: {e}")
            continue
        options = list(case.get("options") or [])
        if case.get("qtype") == "mcq" and options:
            raw = case.get("correct_index")
            got = (ctx.get("basis") or {}).get(
                "index_basis", {}).get("correct_index")
            if (type(raw) is not int or not (0 <= raw < len(options))
                    or got != raw):
                errors.append(
                    f"[{cid}] keyed MCQ needs an in-range integer "
                    f"correct_index, got {raw!r}")
        if "draft" in case or (ctx.get("student") or {}).get("draft"):
            draft = (ctx.get("student") or {}).get("draft") or ""
            if not draft.strip():
                errors.append(f"[{cid}] draft case has an empty draft")
            elif len(_compact(draft)) >= 4:
                visible = " ".join(
                    m.get("content", "")
                    for m in ctx.get("history_rows", []))
                visible += " " + (case.get("message") or "")
                if _compact(draft) in _compact(visible):
                    errors.append(
                        f"[{cid}] draft expression repeats in the visible "
                        f"history/latest message")
    return errors


def _bind_fixture_tools(case, materials):
    """Deterministic, allowlisted tool handlers served from the fixture;
    returns the dispatch handler (caller installs it)."""
    bodies = _CASE_BODIES.get(case["id"], {})
    by_id = {}
    for m in materials:
        info = dict(m)
        info["content"] = bodies.get(m.get("id"), "")
        by_id[m.get("id")] = info

    async def handler(name, owner, args, **kw):
        if name == "list_subjects":
            return {"ok": True, "result": {"subjects": [
                {"id": "fixture", "name": "Synthetic subject", "materials": len(materials),
                 "questions": 1, "questions_due": 0, "questions_new": 0,
                 "cards": 0, "cards_due": 0, "new_per_day": 15,
                 "has_overview": False}]}}
        if name == "list_materials":
            return {"ok": True, "result": {"materials": [
                {"id": m["id"], "name": m["name"], "kind": "text",
                 "category": m.get("category", "theory"),
                 "char_count": len(by_id.get(m["id"], {}).get("content", "")),
                 "page_count": None, "thin_text": False, "has_summary": False,
                 "question_count": 0}
                for m in materials]}}
        if name == "search_materials":
            query = str((args or {}).get("query", ""))
            try:
                rx = re.compile(query, re.IGNORECASE)
            except re.error:
                rx = re.compile(re.escape(query), re.IGNORECASE)
            hits = []
            for m in materials:
                text = by_id.get(m["id"], {}).get("content", "")
                for hit in rx.finditer(text):
                    a, b = max(0, hit.start() - 80), min(len(text), hit.end() + 80)
                    hits.append({"material_id": m["id"], "material": m["name"],
                                 "category": "theory", "page": None,
                                 "snippet": " ".join(text[a:b].split())})
            return {"ok": True, "result": {"hits": hits[:20],
                                           "materials_searched": len(materials)}}
        if name == "get_material":
            info = by_id.get((args or {}).get("material_id"))
            if not info:
                return {"ok": False, "error": "material not found"}
            return {"ok": True, "result": {
                "name": info["name"], "total_chars": len(info["content"]),
                "offset": 0, "text": info["content"],
                "next_offset": None, "category": "theory"}}
        if name == "get_question":
            return {"ok": True, "result": {
                "id": "fixture-question", "number": None, "qtype": case.get("qtype", "open"),
                "question": case["question"],
                "options": case.get("options"), "correct_index": case.get("correct_index"),
                "reference": case.get("private_reference") or "", "topic": None,
                "difficulty": "medium", "state": "new", "suspended": False,
                "material_id": None, "reps": 0, "lapses": 0,
                "origin": "extracted",
                "answer_provenance": {"reference": {"origin": "unknown"},
                                      "correct_index": {"origin": "unknown"}}}}
        if name == "list_questions":
            return {"ok": True, "result": {
                "total": 1, "offset": 0, "next_offset": None,
                "questions": [{"id": "fixture-question", "number": None,
                               "qtype": case.get("qtype", "open"),
                               "topic": None, "difficulty": "medium",
                               "state": "new", "suspended": False,
                               "material_id": None, "reps": 0, "lapses": 0,
                               "question": (case["question"] or "")[:160]}]}}
        if name == "study_stats":
            return {"ok": True, "result": {"today": {}, "recent": []}}
        if name == "study_calibration":
            return {"ok": True, "result": {"curve": [], "count": 0}}
        if name == "list_cards":
            return {"ok": True, "result": {"total": 0, "cards": []}}
        return {"ok": False, "error": f"fixture does not serve {name}"}

    return handler


async def run_case(case: dict, dbcopy: "_TempDbCopy") -> dict:
    from routes import study_routes as sr
    from src import study_agent as sa
    from src import study_practice_coach as coach
    from tests.helpers.sqlite_db import make_temp_sqlite

    import core.database as cdb
    SessionLocal, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
    sa.SessionLocal = SessionLocal
    sr.SessionLocal = SessionLocal            # shim forwards to _common

    context = _fixture_context(case)
    thread = sa.create_thread("evaluator", deck_id="fixture",
                              question_id="fixture")
    # The executor reads the persisted thread, so the fixture's prior turns
    # become the actual conversation history for this run.
    for m in context["history_rows"]:
        sa.save_message("evaluator", thread["id"], m["role"], m["content"])

    dispatch = {"n": 0}
    from src.study_practice_coach import PRACTICE_TOOL_NAMES
    fixture_handler = _bind_fixture_tools(case, context["materials"])
    real_dispatch = sa.dispatch_tool

    async def counting_dispatch(name, owner, args, **kw):
        dispatch["n"] += 1
        # Same allowlist gate the production dispatch applies in practice mode.
        if name not in PRACTICE_TOOL_NAMES:
            return {"ok": False,
                    "error": f"tool '{name}' is not available in practice mode"}
        return await fixture_handler(name, owner, args, **kw)

    sa.dispatch_tool = counting_dispatch

    outcomes = {"reviews": []}
    real_approve = coach.approve_practice_reply

    async def recording_approve(owner, *, candidate, context, history):
        result = await real_approve(owner, candidate=candidate,
                                    context=context, history=history)
        outcomes["reviews"].append({
            "rewritten": bool(result.get("rewritten")),
            "retryable": bool(result.get("retryable")),
        })
        return result

    coach.approve_practice_reply = recording_approve
    started = time.monotonic()
    try:
        events = [c async for c in sa.run_study_agent(
            "evaluator", thread["id"], case["message"],
            practice_context=context)]
    except Exception as e:
        return {"case": case["id"], "error": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.monotonic() - started, 1)}
    finally:
        coach.approve_practice_reply = real_approve
        sa.dispatch_tool = real_dispatch
        engine.dispose()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    parsed = []
    for c in events:
        if not c.startswith("data: "):
            continue
        payload = c[len("data: "):].strip()
        if payload in ("[DONE]",):
            continue
        try:
            parsed.append(json.loads(payload))
        except ValueError:
            pass

    reply_ev = next((e for e in reversed(parsed)
                     if e.get("type") == "reply"), {})
    error_ev = next((e for e in parsed if e.get("type") == "error"), None)
    model_ev = next((e for e in parsed if e.get("type") == "model_info"), {})
    from src.study_practice_coach import FIXED_FALLBACK
    reply = reply_ev.get("content") or (error_ev or {}).get("message") or ""
    if error_ev is not None and not reply_ev:
        # surface the whole stream once: an instant failure after another case
        # is otherwise indistinguishable from a provider error
        print(f"[{case['id']}] raw events: "
              + json.dumps(parsed, ensure_ascii=False)[:2000], file=sys.stderr)
    return {
        "case": case["id"],
        "model": model_ev.get("model"),
        "reply": reply,
        "contained_by_fallback": bool(reply and reply == FIXED_FALLBACK),
        "retryable": bool(reply_ev.get("retryable")),
        "reviews": outcomes["reviews"],
        "tool_calls": dispatch["n"],
        "error": error_ev.get("message") if error_ev else None,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


async def _run_all(cases, dbcopy, verbose):
    results = []
    for case in cases:
        outcome = await run_case(case, dbcopy)
        results.append(outcome)
        summary = outcome.get("error") or (
            "FALLBACK" if outcome.get("contained_by_fallback") else "reply")
        print(f"[{outcome.get('case')}] {summary} "
              f"reviews={outcome.get('reviews')} "
              f"tools={outcome.get('tool_calls')} "
              f"t={outcome.get('elapsed_s')}s")
        if verbose:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return results


def main() -> int:
    import logging
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(REPO / "tests" / "fixtures" /
                                           "study_practice_coach_cases.json"))
    ap.add_argument("--db", default=str(REPO / "data" / "app.db"),
                    help="app DB to COPY read-only for model resolution "
                         "(never touched in place)")
    ap.add_argument("--output", default=str(Path(tempfile.gettempdir()) /
                                           "practice_coach_eval.json"))
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--only", default="",
                    help="comma-separated case ids to rerun (still at most 8)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.only.strip():
        wanted = {c.strip() for c in args.only.split(",") if c.strip()}
        cases = [c for c in cases if c.get("id") in wanted]
    limit = max(1, min(8, args.limit))  # hard cap: 8 cases per invocation
    cases = cases[:limit]

    preflight_errors = _preflight_fixture_contexts(cases)
    if preflight_errors:
        for err in preflight_errors:
            print(f"preflight: {err}", file=sys.stderr)
        return 2

    source = Path(args.db)
    if not source.exists():
        print(f"note: --db {source} not found; model resolution starts from "
              "a blank database (no endpoint rows — the run will report that)")
    dbcopy = _TempDbCopy(source if source.exists() else None)
    try:
        # One event loop for the whole run: the provider client is loop-bound,
        # and a fresh asyncio.run per case reuses it across closed loops.
        results = asyncio.run(_run_all(cases, dbcopy, args.verbose))
    finally:
        dbcopy.close()

    Path(args.output).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nresults written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())