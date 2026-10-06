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
    try:
        response = await llm_call_async(
            url, model,
            messages,
            headers=headers,
            timeout=AI_CHAT_TIMEOUT,
        )
        # Truncate very long responses
        if len(response) > 10000:
            response = response[:10000] + "\n... (truncated)"
        result = {"model": model, "response": response}
        entry = await asyncio.to_thread(_roster_entry_for, model, owner)
        if entry is not None:
            result["cost"] = entry.cost_label()
            result["tier"] = entry.tier
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

    try:
        response = await llm_call_async(
            url, model,
            [
                {"role": "system", "content": _TEACHER_SYSTEM_PROMPT},
                {"role": "user", "content": f"Problem:\n{problem}"},
            ],
            headers=headers,
            timeout=AI_CHAT_TIMEOUT,
        )
        if len(response) > 8000:
            response = response[:8000] + "\n... (truncated)"
        return {"model": model, "response": response, "teacher": True}
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
