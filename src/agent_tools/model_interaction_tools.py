"""model_interaction_tools.py - agent tools for talking to other models.

Owns the model-interaction tool implementations (chat_with_model, ask_teacher,
list_models) and their handler classes, registered in ``TOOL_HANDLERS``. Part
of the tool -> registry migration (#3629): the implementations were moved here
out of ``src.ai_interaction`` so dispatch flows through the registry instead of
the elif chain / dispatch_ai_tool in tool_execution.py.

Shared helpers that still live in ``src.ai_interaction`` and are used by tools
not yet migrated (``_resolve_model``, ``AI_CHAT_TIMEOUT``) are imported lazily
inside the functions to avoid an import cycle at module load.
"""
import asyncio
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


_TEACHER_SYSTEM_PROMPT = (
    "You are a senior AI mentor. A less capable model is stuck on a problem and asking for help. "
    "Provide clear, actionable guidance:\n"
    "1. Brief analysis of the problem\n"
    "2. Recommended approach (step by step)\n"
    "3. Key things to watch out for\n\n"
    "Be concise and practical. No preamble."
)


def _parse_delegate_content(content: str) -> tuple[str, str, str]:
    """(model_spec, message, instructions) from the tool content.

    Plain form: line 1 = model, rest = message. Native calls that carry
    ``instructions`` arrive as a JSON object instead.
    """
    import json

    text = (content or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return (str(data.get("model") or "").strip(), str(data.get("message") or "").strip(),
                    str(data.get("instructions") or "").strip())
    lines = text.split("\n", 1)
    return (lines[0].strip() if lines else "", lines[1].strip() if len(lines) > 1 else "", "")


def _roster_entry_for(model_id: str, owner: Optional[str]):
    try:
        from src import model_roster

        return next((e for e in model_roster.roster(owner) if e.model == model_id), None)
    except Exception:
        return None


def _cheaper_options(owner: Optional[str], exclude_model: str, limit_usd: Optional[float],
                     input_tokens: int, output_tokens: int, n: int = 3) -> list:
    """Roster keys that fit the budget: free/flat-rate first, then priced metered models."""
    try:
        from src import model_roster

        entries = model_roster.roster(owner)
    except Exception:
        return []
    picks = []
    for entry in entries:
        if entry.model == exclude_model:
            continue
        if entry.billing in ("subscription", "local"):
            picks.append((0, not entry.recommended, 0.0, entry))
            continue
        if entry.input_per_mtok is None:
            continue
        out_price = entry.output_per_mtok if entry.output_per_mtok is not None else entry.input_per_mtok
        cost = (input_tokens * entry.input_per_mtok + output_tokens * out_price) / 1_000_000
        if limit_usd is None or cost <= limit_usd:
            picks.append((1, not entry.recommended, cost, entry))
    picks.sort(key=lambda p: p[:3])
    return [f"{e.key} ({'free' if e.billing == 'local' else 'plan' if e.billing == 'subscription' else f'~${c:.3f}'})"
            for _, _, c, e in picks[:n]]


def _budget_guard(owner: Optional[str], url: str, model: str, messages: list, what: str):
    """(price, estimate_usd, refusal) for a delegation; refusal is a tool error dict or None.

    Metered calls over the single-action limit are refused rather than asked
    about: the agent cannot approve its own spend, so it is pointed at cheaper
    models or told to ask the user to raise the limit. The monthly cap (block
    mode) refuses any metered call that would cross it.
    """
    from src import budget

    try:
        price = budget.price_for(url, model, budget.endpoint_kind_for_url(url))
        if not price.metered:
            return price, None, None
        tokens_in = budget.estimate_tokens(messages)
        tokens_out = budget.EXPECTED_OUTPUT_TOKENS["delegation"]
        estimate = price.cost(tokens_in, tokens_out)
        decision = budget.check(owner, estimate, what=what)
    except Exception as exc:
        # Guardrails fail open: a broken budget store must not stop delegation.
        logger.warning("budget guard skipped: %s", exc)
        return None, None, None
    if decision.allowed and not decision.confirm:
        return price, estimate, None
    limit = decision.settings.get("action_limit_usd") or None
    if not decision.allowed:
        cap = decision.settings.get("monthly_cap_usd") or 0.0
        limit = max(cap - decision.spent_usd, 0.0) if cap else limit
    options = _cheaper_options(owner, model, limit, tokens_in, tokens_out)
    hint = f" Options within budget: {', '.join(options)}." if options else ""
    tail = ("" if not decision.allowed else
            " You cannot approve this yourself: use a cheaper model, or ask the user to raise the "
            "single-action limit in Settings → Budget.")
    return price, estimate, {"error": f"Budget: {decision.reason}{tail}{hint}", "budget_blocked": True}


def _record_delegation(owner: Optional[str], session_id: Optional[str], price, model: str,
                       messages: list, response: str, source: str) -> Optional[float]:
    """Bill a finished delegation from its prompt and reply (token estimates)."""
    if price is None or not price.metered:
        return None
    from src import budget

    usage = {"input_tokens": budget.estimate_tokens(messages), "output_tokens": budget.text_tokens(response)}
    return budget.record(owner, source=source, model=model, usage=usage, price=price,
                         session_id=session_id, estimated=True)


async def chat_with_model(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Delegate to (or consult) another configured model and return its answer.

    Content: line 1 = model (a roster key ``endpoint_id::model`` from
    list_models, a model name, or ``model@endpoint``); line 2+ = the message.
    Native calls may also pass ``instructions``: a system prompt for the
    delegate (its role, constraints, output format).
    """
    from src.ai_interaction import _resolve_model, AI_CHAT_TIMEOUT
    from src.llm_core import llm_call_async

    model_spec, message, instructions = _parse_delegate_content(content)
    if not model_spec:
        return {"error": "First line must be the model name (see list_models for exact keys)"}
    if not message:
        return {"error": "No message provided (line 2+ is the message)"}

    try:
        url, model, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner)
    except ValueError as e:
        return {"error": f"{e}. Call list_models to see the exact keys."}

    messages = [{"role": "user", "content": message}]
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions[:8000]})
    price, _estimate, refusal = await asyncio.to_thread(
        _budget_guard, owner, url, model, messages, f"Delegating to {model}")
    if refusal:
        return refusal
    try:
        response = await llm_call_async(
            url, model,
            messages,
            headers=headers,
            timeout=AI_CHAT_TIMEOUT,
        )
        spent = await asyncio.to_thread(_record_delegation, owner, session_id, price, model,
                                        messages, response or "", "delegation")
        # Truncate very long responses
        if len(response) > 10000:
            response = response[:10000] + "\n... (truncated)"
        result = {"model": model, "response": response}
        entry = await asyncio.to_thread(_roster_entry_for, model, owner)
        if entry is not None:
            result["cost"] = entry.cost_label()
            result["tier"] = entry.tier
        if spent is not None:
            result["spent_usd"] = round(spent, 6)
        return result
    except Exception as e:
        logger.error(f"chat_with_model failed: {e}")
        return {
            "error": f"Failed to get response from {model_spec}: {e}",
            "untrusted_content": True,
        }


def _auto_teacher(owner: Optional[str]) -> str:
    """A recommended flagship model from the roster, for ask_teacher 'auto'."""
    try:
        from src import model_roster

        entries = model_roster.roster(owner)
    except Exception:
        return ""
    for pick in (
        lambda e: e.recommended and e.tier == "flagship",
        lambda e: e.recommended,
        lambda e: e.tier == "flagship",
    ):
        hit = next((e for e in entries if pick(e)), None)
        if hit:
            return hit.key
    return ""


async def ask_teacher(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Ask a more capable model for help.

    Content format:
      Line 1: model_name (or 'auto')
      Line 2+: the problem description
    """
    from src.ai_interaction import _resolve_model, AI_CHAT_TIMEOUT
    from src.llm_core import llm_call_async
    from src.settings import get_setting

    lines = content.strip().split("\n", 1)
    model_spec = lines[0].strip() if lines else "auto"
    problem = lines[1].strip() if len(lines) > 1 else ""

    if not problem:
        return {"error": "No problem description provided"}

    if model_spec.lower() in ("auto", ""):
        model_spec = get_setting("teacher_model", "") or await asyncio.to_thread(_auto_teacher, owner)
        if not model_spec:
            return {"error": "No teacher model available. Specify a model (see list_models) or set teacher_model in settings."}

    try:
        url, model, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner)
    except ValueError as e:
        return {"error": str(e)}

    messages = [
        {"role": "system", "content": _TEACHER_SYSTEM_PROMPT},
        {"role": "user", "content": f"Problem:\n{problem}"},
    ]
    price, _estimate, refusal = await asyncio.to_thread(
        _budget_guard, owner, url, model, messages, f"Asking {model} for help")
    if refusal:
        return refusal
    try:
        response = await llm_call_async(
            url, model,
            messages,
            headers=headers,
            timeout=AI_CHAT_TIMEOUT,
        )
        spent = await asyncio.to_thread(_record_delegation, owner, session_id, price, model,
                                        messages, response or "", "teacher")
        if len(response) > 8000:
            response = response[:8000] + "\n... (truncated)"
        result = {"model": model, "response": response, "teacher": True}
        if spent is not None:
            result["spent_usd"] = round(spent, 6)
        return result
    except Exception as e:
        logger.error(f"ask_teacher failed: {e}")
        return {
            "error": f"Teacher call failed ({model_spec}): {e}",
            "untrusted_content": True,
        }


async def list_models(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Every model this user can delegate to: recommended, kind, tier, cost.

    Content = optional filter keyword (matches model, endpoint, kind, tier or
    strength, e.g. "code", "fast", "local", "claude").
    """
    from src import model_roster

    keyword = content.strip().lower() if content.strip() else None
    try:
        # Owner-scoped: roster() lists only endpoints visible to this owner.
        entries = await asyncio.to_thread(model_roster.roster, owner)
    except Exception as e:
        logger.error(f"list_models failed: {e}")
        return {"error": "Could not list models."}
    if keyword:
        def _hay(e) -> str:
            return " ".join([e.model, e.endpoint_name, e.kind, e.tier, *e.traits,
                             "recommended" if e.recommended else ""]).lower()
        entries = [e for e in entries if keyword in _hay(e)]
    if not entries:
        if not keyword:
            return {"results": "No enabled model endpoints configured."}
        return {"results": f"No models found matching '{keyword}'."}
    header = (f"Available models ({len(entries)}). Pass the [key] as `model` to chat_with_model "
              "to delegate a subtask or get a second opinion.")
    return {"results": header + "\n" + model_roster.roster_lines(entries, limit=80)
            + "\n\n" + model_roster.ROUTING_GUIDANCE}


# ---------------------------------------------------------------------------
# Handler classes registered in TOOL_HANDLERS
# ---------------------------------------------------------------------------

class ChatWithModelTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await chat_with_model(content, ctx.get("session_id"), owner=ctx.get("owner"))


class AskTeacherTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await ask_teacher(content, ctx.get("session_id"), owner=ctx.get("owner"))


class ListModelsTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await list_models(content, ctx.get("session_id"), owner=ctx.get("owner"))
