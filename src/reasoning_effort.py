"""Reasoning effort: which levels a model accepts, who chose one, how to send it.

A level comes from one of two places:

- **The place that picked the model.** A Council seat, a Compare column, the
  Study options and the Utility / Study / Vision roles in Settings each keep
  their own level. It travels with that place's request headers as
  ``EFFORT_HEADER``; llm_core removes the header before any request leaves the
  process.
- **The model itself** (chat composer -> Effort). Saved per user under
  ``model_effort`` ({model id: level}) and applied by ``llm_core`` to every
  *streamed* call of that model made inside ``for_owner``: chat and agent
  turns, Council seats and the Study tutor. Background one-shot calls (titles,
  memory extraction, Study question generation) keep the provider default, so
  a "max" chat setting never makes housekeeping slow or expensive.

A place's own level wins over the model's. One scale serves every provider:

    none < minimal < low < medium < high < xhigh < max

A level a model does not accept is clamped to the nearest lower one it does
(``xhigh`` on Claude Opus 4.6 sends ``high``), so a choice left over from
another model never causes a 400. A model with no effort control gets nothing
sent, and the selectors hide for it.

How each provider takes it:

- Claude: ``output_config.effort`` (API) / ``--effort`` (subscription CLI),
  levels per model family from Anthropic's model table.
- ChatGPT Subscription (Codex): ``reasoning.effort``, levels exactly as the
  Codex models endpoint reports them per model.
- OpenRouter: ``reasoning.effort``.
- OpenAI (gpt-5.x, o-series), Gemini 2.5+, Mistral thinking models and gpt-oss
  on any server: ``reasoning_effort`` (Ollama native: ``think``).
"""

from __future__ import annotations

import contextvars
import json
import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import urlparse

LEVELS: Tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
LABELS: Dict[str, str] = {
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Max",
}
EFFORT_HEADER = "X-Odysseus-Reasoning-Effort"
PREF_KEY = "model_effort"

_CLAUDE_ALL = ("low", "medium", "high", "xhigh", "max")
_UP_TO_HIGH = ("low", "medium", "high")
_UP_TO_XHIGH = ("low", "medium", "high", "xhigh")
_MISTRAL = ("none", "low", "medium", "high")
_CLAUDE_RE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")
# Claude Code CLI aliases resolve to the newest model of the family.
_CLAUDE_CLI_ALIASES = {"opus", "sonnet", "fable", "default", "best", "opusplan"}
_OPENROUTER_REASONING_RE = re.compile(
    r"(^|/)(o\d|gpt-5|gpt-oss|claude|gemini-(2\.5|[3-9])|grok|deepseek-r|qwq|qwen3|magistral|kimi-k2-thinking)")

# {model id: level} for the user whose request is running; None = not in scope.
_SAVED: "contextvars.ContextVar[Optional[Dict[str, str]]]" = contextvars.ContextVar(
    "odysseus_model_effort", default=None
)


def normalize(value: Any) -> Optional[str]:
    level = str(value or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    level = {"extrahigh": "xhigh", "maximum": "max", "med": "medium"}.get(level, level)
    return level if level in LEVELS else None


def _host_is(url: str, domain: str) -> bool:
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    return host == domain or host.endswith("." + domain)


# ── Which levels a model accepts ──

def _claude_levels(model: str) -> Tuple[Tuple[str, ...], Optional[str]]:
    m = model.lower().rsplit("/", 1)[-1]
    if m in _CLAUDE_CLI_ALIASES:
        return _CLAUDE_ALL, None
    match = _CLAUDE_RE.search(m)
    if not match:
        return (), None
    family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    if major >= 5:
        default = "medium" if (family, major, minor) in (("opus", 5, 5), ("haiku", 5, 5)) else "high"
        return _CLAUDE_ALL, default
    if major == 4 and minor >= 7:
        return _CLAUDE_ALL, "high"
    if major == 4 and minor == 6 and family in ("opus", "sonnet"):
        return ("low", "medium", "high", "max"), "high"
    if major == 4 and minor == 5 and family == "opus":
        return _UP_TO_HIGH, "high"
    return (), None  # Sonnet 4.5, Haiku 4.5 and older reject effort


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


def levels_for(provider: str, url: str, model: str) -> Tuple[Tuple[str, ...], Optional[str]]:
    """(levels the model accepts, provider default level or None) for a model
    served by ``provider`` (``llm_core._detect_provider(url)``) at ``url``.
    Empty levels mean no effort control."""
    model = (model or "").strip()
    if not model:
        return (), None
    lower = model.lower()
    if provider in ("anthropic", "claude-subscription"):
        return _claude_levels(model)
    if provider == "chatgpt-subscription":
        from src.chatgpt_subscription import reasoning_levels
        info = reasoning_levels(model)
        if info:
            return tuple(lv for lv in info["levels"] if lv in LEVELS), info.get("default")
        # The Codex backend serves only reasoning models.
        return _openai_levels(lower) or _UP_TO_HIGH, None
    if provider == "openrouter":
        # OpenRouter normalises `reasoning.effort` across vendors and ignores it
        # for models without reasoning; offer it where reasoning is likely.
        return (_UP_TO_HIGH, None) if _OPENROUTER_REASONING_RE.search(lower) else ((), None)
    if provider == "mistral":
        from src.llm_core import _MISTRAL_REASONING_EFFORT, _supports_thinking
        return (_MISTRAL, _MISTRAL_REASONING_EFFORT) if _supports_thinking(model) else ((), None)
    # gpt-oss takes reasoning_effort (or Ollama's think level) on every server
    # that runs it: vLLM, llama.cpp, Ollama, Groq, Cerebras, W&B, ...
    if "gpt-oss" in lower:
        return _UP_TO_HIGH, "medium"
    if provider == "openai":
        if _host_is(url, "openai.com"):
            levels = _openai_levels(lower)
            return levels, ("medium" if levels else None)
        if _host_is(url, "generativelanguage.googleapis.com") and re.search(r"gemini-(2\.5|[3-9])", lower):
            return _UP_TO_HIGH, None
    return (), None


def levels_for_url(url: str, model: str) -> Tuple[Tuple[str, ...], Optional[str]]:
    from src.llm_core import _detect_provider
    return levels_for(_detect_provider(url), url, model)


def clamp(level: Any, supported: Tuple[str, ...]) -> Optional[str]:
    """The highest supported level at or below ``level`` (lowest supported if none)."""
    level = normalize(level)
    if not level or not supported:
        return None
    if level in supported:
        return level
    rank = LEVELS.index(level)
    below = [s for s in supported if s in LEVELS and LEVELS.index(s) <= rank]
    return below[-1] if below else supported[0]


def effective(provider: str, url: str, model: str, effort: Any) -> Optional[str]:
    """``effort`` clamped to what this model accepts (None = provider default)."""
    return clamp(effort, levels_for(provider, url, model)[0])


# ── A place's own level, carried in its headers ──

def with_effort(headers: Any, effort: Any) -> Dict[str, Any]:
    """A copy of ``headers`` carrying ``effort`` (or none, when it is unset)."""
    _, out = take_effort(headers)
    out = dict(out) if isinstance(out, dict) else {}
    level = normalize(effort)
    if level:
        out[EFFORT_HEADER] = level
    return out


def session_headers(sess: Any) -> Dict[str, Any]:
    """A chat session's headers plus its own level, if it has one (Compare
    columns do; ordinary chats use the model's saved level instead)."""
    return with_effort(getattr(sess, "headers", None), getattr(sess, "reasoning_effort", None))


def take_effort(headers: Any) -> Tuple[Optional[str], Any]:
    """Split the effort marker off ``headers``: (effort, headers without it).

    Headers that arrive JSON-encoded (some sessions stored them that way) are
    decoded first; anything that is not a mapping is passed through untouched.
    """
    if isinstance(headers, str):
        try:
            decoded = json.loads(headers)
        except ValueError:
            return None, headers
        if not isinstance(decoded, dict):
            return None, headers
        headers = decoded
    if not isinstance(headers, dict):
        return None, headers
    effort = None
    rest = {}
    for key, value in headers.items():
        if str(key).lower() == EFFORT_HEADER.lower():
            effort = normalize(value)
        else:
            rest[key] = value
    return effort, rest


# ── The model's own level, saved per user ──

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
        level = normalize(level)
        if isinstance(model, str) and model and level:
            out[model] = level
    return out


def save_user_level(owner: Optional[str], model: str, level: Optional[str]) -> Dict[str, str]:
    """Set (or with ``level=None`` clear) the user's effort for ``model``."""
    from routes.prefs_routes import _load_for_user, _save_for_user

    prefs = _load_for_user(owner or None) or {}
    levels = user_levels(owner)
    level = normalize(level)
    if level:
        levels[model] = level
    else:
        levels.pop(model, None)
    prefs[PREF_KEY] = levels
    _save_for_user(owner or None, prefs)
    return levels


@contextmanager
def _scope(levels: Optional[Dict[str, str]]) -> Iterator[None]:
    token = _SAVED.set(levels)
    try:
        yield
    finally:
        try:
            _SAVED.reset(token)
        except ValueError:
            # An async generator resumed from a different context than it
            # started in; the value dies with that context anyway.
            pass


@contextmanager
def for_owner(owner: Optional[str]) -> Iterator[Dict[str, str]]:
    """Apply ``owner``'s saved per-model levels to the streamed calls made inside."""
    levels = user_levels(owner)
    with _scope(levels):
        yield levels


@contextmanager
def without_saved() -> Iterator[None]:
    """Keep saved per-model levels out of the calls made inside (one-shot
    background calls that happen to stream under the hood)."""
    with _scope(None):
        yield


def saved_level(model: str) -> Optional[str]:
    """The in-scope user's saved level for ``model`` (not yet clamped)."""
    levels = _SAVED.get()
    return levels.get(model) if levels and model else None


def resolve(provider: str, url: str, model: str, headers: Any) -> Tuple[Optional[str], Any]:
    """The level to send for a streamed call, and ``headers`` without the marker.

    The asking place's own level wins, else the user's saved level for the
    model; either is clamped to what the model accepts.
    """
    explicit, rest = take_effort(headers)
    return effective(provider, url, model, explicit or saved_level(model)), rest


# ── Writing it into a request ──

def apply(payload: Dict[str, Any], provider: str, effort: Optional[str]) -> None:
    """Write an already-clamped level into a provider request body."""
    if not effort:
        return
    if provider == "anthropic":
        payload["output_config"] = {**(payload.get("output_config") or {}), "effort": effort}
    elif provider in ("chatgpt-subscription", "openrouter"):
        payload["reasoning"] = {**(payload.get("reasoning") or {}), "effort": effort}
    elif provider == "ollama":
        # Native /api/chat: gpt-oss reads the level from `think`.
        payload["think"] = effort
    else:
        payload["reasoning_effort"] = effort
