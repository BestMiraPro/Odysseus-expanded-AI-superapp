"""Per-model reasoning effort: what each provider accepts and how to send it.

A user picks an effort level per model (chat composer -> Effort). The choice
is stored in that user's prefs under ``model_effort`` ({model id: level}) and
applied by ``llm_core`` to every *streamed* call of that model made while the
user's levels are in scope: chat and agent turns, Council seats and the Study
tutor. Background one-shot calls (titles, memory extraction, Study question
generation) go through ``llm_call_async`` and keep the provider default, so a
"max" chat setting never makes housekeeping slow or expensive.

One scale for every provider, mapped onto what each one accepts:

    low < medium < high < xhigh < max

A level a model does not accept is clamped to the nearest lower one it does
(``xhigh`` on Claude Opus 4.6 sends ``high``); a model with no effort control
gets nothing sent and the composer hides the selector for it.
"""

from __future__ import annotations

import contextvars
import re
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Tuple
from urllib.parse import urlparse

LEVELS: Tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
LABELS: Dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Max",
}
PREF_KEY = "model_effort"

_UP_TO_HIGH = ("low", "medium", "high")
_UP_TO_XHIGH = ("low", "medium", "high", "xhigh")

# {model id: level} for the user whose request is running; None = not in scope.
_LEVELS: "contextvars.ContextVar[Optional[Dict[str, str]]]" = contextvars.ContextVar(
    "odysseus_model_effort", default=None
)


def normalize_level(value) -> Optional[str]:
    level = str(value or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    level = {"extrahigh": "xhigh", "maximum": "max", "med": "medium"}.get(level, level)
    return level if level in LEVELS else None


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def _host_is(url: str, domain: str) -> bool:
    host = _host(url)
    return host == domain or host.endswith("." + domain)


def _claude_levels(model: str) -> Tuple[str, ...]:
    """Effort levels the Claude API accepts for ``model`` (``output_config.effort``)."""
    from src.llm_core import _CLAUDE_FAMILY_VERSION_RE, _CLAUDE_GENERIC_FAMILY_VERSION_RE

    lowered = (model or "").lower()
    match = _CLAUDE_FAMILY_VERSION_RE.search(lowered) or _CLAUDE_GENERIC_FAMILY_VERSION_RE.search(lowered)
    if not match:
        return ()
    family = match.group(1)
    version = (int(match.group(2)), int(match.group(3)) if match.group(3) else 0)
    if version[0] >= 5 or (family == "opus" and version >= (4, 7)):
        return LEVELS
    if family in ("opus", "sonnet") and version == (4, 6):
        return ("low", "medium", "high", "max")
    if family == "opus" and version == (4, 5):
        return _UP_TO_HIGH
    # Sonnet 4.5, Haiku 4.5 and older reject the parameter.
    return ()


# Claude Code CLI aliases resolve to the newest model of the family.
_CLAUDE_CLI_ALIASES = {"opus", "sonnet", "fable", "default", "best", "opusplan"}


def _openai_levels(model: str) -> Tuple[str, ...]:
    """``reasoning_effort`` levels for OpenAI reasoning models (gpt-5.x, o-series)."""
    m = (model or "").lower().split("/")[-1]
    if re.match(r"^o\d", m):
        return _UP_TO_HIGH
    match = re.match(r"^gpt-(\d+)(?:\.(\d+))?", m)
    if not match:
        return ()
    version = (int(match.group(1)), int(match.group(2) or 0))
    if version < (5, 0):
        return ()
    # xhigh arrived with gpt-5.1-codex-max and is standard from gpt-5.2 on.
    if version >= (5, 2) or "codex-max" in m:
        return _UP_TO_XHIGH
    return _UP_TO_HIGH


def supported_levels(provider: str, model: str, url: str = "") -> Tuple[str, ...]:
    """Effort levels ``model`` accepts on ``provider`` (``()`` = no effort control).

    ``provider`` is ``llm_core._detect_provider(url)``.
    """
    m = (model or "").lower()
    if not m:
        return ()
    if provider == "claude-subscription":
        if m in _CLAUDE_CLI_ALIASES:
            return LEVELS
        return _claude_levels(m)
    if provider == "anthropic":
        return _claude_levels(m)
    if provider == "chatgpt-subscription":
        # The Codex backend serves only reasoning models.
        return _openai_levels(m) or _UP_TO_HIGH
    if provider == "openrouter":
        # OpenRouter normalises `reasoning.effort` across vendors and ignores it
        # for models without reasoning; offer it where reasoning is likely.
        if re.search(r"(^|/)(o\d|gpt-5|gpt-oss|claude|gemini-(2\.5|[3-9])|grok|deepseek-r|qwq|qwen3|magistral|kimi-k2-thinking)", m):
            return _UP_TO_HIGH
        return ()
    if provider == "mistral":
        from src.llm_core import _supports_thinking

        return _UP_TO_HIGH if _supports_thinking(m) else ()
    # gpt-oss takes reasoning_effort (or Ollama's think level) on every server
    # that runs it: vLLM, llama.cpp, Ollama, Groq, Cerebras, W&B, ...
    if "gpt-oss" in m:
        return _UP_TO_HIGH
    if provider == "openai":
        if _host_is(url, "openai.com"):
            return _openai_levels(m)
        if _host_is(url, "generativelanguage.googleapis.com") and re.search(r"gemini-(2\.5|[3-9])", m):
            return _UP_TO_HIGH
    return ()


def clamp(level: Optional[str], supported: Tuple[str, ...]) -> Optional[str]:
    """The highest supported level at or below ``level`` (lowest supported if none)."""
    level = normalize_level(level)
    if not level or not supported:
        return None
    if level in supported:
        return level
    rank = LEVELS.index(level)
    below = [s for s in supported if LEVELS.index(s) <= rank]
    return below[-1] if below else supported[0]


# ── Per-user storage ──

def user_levels(owner: Optional[str]) -> Dict[str, str]:
    """The user's saved {model id: level} map (empty if none)."""
    try:
        from routes.prefs_routes import _load_for_user

        raw = (_load_for_user(owner or None) or {}).get(PREF_KEY)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for model, level in raw.items():
        level = normalize_level(level)
        if isinstance(model, str) and model and level:
            out[model] = level
    return out


def save_user_level(owner: Optional[str], model: str, level: Optional[str]) -> Dict[str, str]:
    """Set (or with ``level=None`` clear) the user's effort for ``model``."""
    from routes.prefs_routes import _load_for_user, _save_for_user

    prefs = _load_for_user(owner or None) or {}
    levels = user_levels(owner)
    level = normalize_level(level)
    if level:
        levels[model] = level
    else:
        levels.pop(model, None)
    prefs[PREF_KEY] = levels
    _save_for_user(owner or None, prefs)
    return levels


# ── Request scope ──

@contextmanager
def for_owner(owner: Optional[str]) -> Iterator[Dict[str, str]]:
    """Apply ``owner``'s saved effort levels to the streamed calls made inside."""
    levels = user_levels(owner)
    token = _LEVELS.set(levels)
    try:
        yield levels
    finally:
        try:
            _LEVELS.reset(token)
        except ValueError:
            # An async generator resumed from a different context than it
            # started in; the value dies with that context anyway.
            pass


def level_for(provider: str, model: str, url: str = "") -> Optional[str]:
    """The effort to send for this call, already clamped to what the model accepts."""
    levels = _LEVELS.get()
    if not levels or not model:
        return None
    level = levels.get(model)
    if not level:
        return None
    return clamp(level, supported_levels(provider, model, url))


def apply_to_payload(provider: str, model: str, payload: Dict, url: str = "") -> Optional[str]:
    """Write the in-scope effort for ``model`` into a provider request body."""
    level = level_for(provider, model, url)
    if not level:
        return None
    if provider == "anthropic":
        payload["output_config"] = {**(payload.get("output_config") or {}), "effort": level}
    elif provider in ("chatgpt-subscription", "openrouter"):
        payload["reasoning"] = {**(payload.get("reasoning") or {}), "effort": level}
    elif provider == "ollama":
        # Native /api/chat: gpt-oss reads the level from `think`.
        payload["think"] = level
    else:
        payload["reasoning_effort"] = level
    return level
