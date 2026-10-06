"""One roster of every model this user can call, with what it costs and which to prefer.

Used wherever a person or a model has to choose between models: the Council
seat picker, the Omnigent crew orchestrator's prompt, and the chat agent's
``ask_model`` tool. Each entry says:

* **kind** - ``subscription`` (Claude/ChatGPT/Copilot plans), ``api`` (metered)
  or ``local`` (your own hardware).
* **recommended** - the newest model of its family, unless that whole family
  is a generation behind its vendor's current line. Derived from the model id
  (and release dates when the price catalog knows them), so it stays right as
  new models are added without anyone editing a list.
* **cost** - per million tokens. Sources, in order: the operator's declared
  file (``data/omnigent-model-costs.json``, shared with the Omnigent catalog),
  then public list prices from OpenRouter's model catalog (cached daily).
  Nothing is invented: an unmatched model says "price unknown".
* **class** - flagship / balanced / fast, plus traits (code, reasoning, vision),
  so an orchestrator can match task weight to model weight.

Set ``ODYSSEUS_MODEL_PRICES=off`` to never fetch the public catalog, or
``ODYSSEUS_MODEL_PRICES_URL`` to point at another OpenRouter-format catalog.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SUBSCRIPTION_PROVIDERS = {"claude-subscription", "chatgpt-subscription", "copilot"}

DEFAULT_PRICES_URL = "https://openrouter.ai/api/v1/models"
PRICE_CACHE_TTL = 24 * 3600
_PRICE_FETCH_MAX_BYTES = 20 * 1024 * 1024
# A release this much older than its vendor's newest is a previous generation.
_STALE_AFTER_SECONDS = 540 * 24 * 3600

# Words that name a distinct product line within a vendor (kept in the family).
_VARIANTS = {
    "opus", "sonnet", "haiku", "fable", "mini", "nano", "flash", "pro", "lite",
    "turbo", "max", "plus", "ultra", "coder", "code", "codex", "chat",
    "reasoner", "thinking", "vision", "vl", "large", "medium", "small", "air",
    "omni", "o", "r", "k", "v", "next", "scout", "maverick", "devstral", "magistral",
}
# Words that only mark a release flavour (dropped from the family).
_FLAVOURS = {"latest", "preview", "exp", "experimental", "beta", "free", "hf", "gguf", "instruct",
             "fp8", "fp16", "bf16", "awq", "gptq", "mlx", "q4", "q5", "q6", "q8", "it"}
# Organisation prefixes some ids carry before the model name.
_ORGS = {"meta", "mistralai", "google", "microsoft", "nvidia", "ibm", "zai", "zai-org",
         "z-ai", "moonshotai", "deepseek-ai", "qwen", "openai", "anthropic", "x-ai", "xai"}

_FLAGSHIP_WORDS = {"opus", "pro", "ultra", "max", "large", "fable"}
_FAST_WORDS = {"haiku", "mini", "nano", "flash", "lite", "small", "air", "turbo"}

_TRAITS = (
    (r"coder|[-_]code\b|codex|devstral", "code"),
    (r"thinking|reason|[-_]r1\b|^o\d", "reasoning"),
    (r"vision|[-_]vl\b|omni|4o\b", "vision"),
)


# ---------------------------------------------------------------------------
# Parsing model ids
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelIdentity:
    line: str                      # vendor line: claude, gpt, qwen, deepseek, ...
    family: str                    # line + product variant (+ size tier for open models)
    version: Tuple[int, ...]       # (5, 5) for claude-opus-5-5, () when none
    stamp: int                     # date stamp from the id (20251001, 2411), 0 when none
    params_b: Optional[float]      # total parameters in billions when the id says
    variants: Tuple[str, ...]


_SIZE_RE = re.compile(r"^(?:a)?(\d+(?:\.\d+)?)([bm])$")
_NUM_RE = re.compile(r"^([a-z]*)(\d+(?:\.\d+)*)([a-z]*)$")


def _size_tier(params_b: Optional[float]) -> str:
    if params_b is None:
        return ""
    if params_b < 14:
        return "small"
    if params_b < 80:
        return "mid"
    if params_b < 300:
        return "large"
    return "frontier"


def parse_model_id(model_id: str) -> ModelIdentity:
    """Vendor line, family, version and size read out of a model id.

    ``claude-opus-5-5`` -> claude / claude-opus / (5, 5)
    ``claude-3-5-sonnet-20241022`` -> claude / claude-sonnet / (3, 5), stamp 20241022
    ``gpt-5.5-mini`` -> gpt / gpt-mini / (5, 5)
    ``Qwen/Qwen3-Coder-480B-A35B`` -> qwen / qwen-coder@frontier / (3,)
    ``deepseek-v4-flash`` -> deepseek / deepseek-flash / (4,)
    ``mistral-large-2411`` -> mistral / mistral-large / (), stamp 20241100
    """
    base = (model_id or "").strip().lower().rsplit("/", 1)[-1]
    base = re.sub(r"\[.*?\]", "", base)                 # claude-sonnet-5-5[1m]
    base = re.sub(r"[_\s:@]+", "-", base).strip("-")
    stamp = 0
    m = re.search(r"-(20\d{2})-?(\d{2})-?(\d{2})$", base)
    if m:
        stamp = int("".join(m.groups()))
        base = base[: m.start()]
    else:
        m = re.search(r"-(2\d[01]\d)$", base)            # YYMM, e.g. mistral-large-2411
        if m:
            stamp = 20000000 + int(m.group(1)) * 100
            base = base[: m.start()]

    names: List[str] = []
    variants: List[str] = []
    version: List[int] = []
    params: List[float] = []
    version_closed = False
    for tok in [t for t in base.split("-") if t]:
        if re.fullmatch(r"a\d+(?:\.\d+)?b", tok):          # MoE active params (A35B)
            continue
        size = re.fullmatch(r"(\d+(?:\.\d+)?)([bm])", tok)
        if size:
            params.append(float(size.group(1)) / (1000 if size.group(2) == "m" else 1))
            continue
        if tok in _FLAVOURS:
            continue
        num = _NUM_RE.match(tok)
        if num:
            lead, digits, trail = num.groups()
            if lead and lead != "v":
                (variants if (names and lead in _VARIANTS) else names).append(lead)
            if len(digits) < 3 or "." in digits:            # skip 002 / 0613 revisions
                if not version_closed:
                    version.extend(int(x) for x in digits.split("."))
            if trail:
                variants.append(trail)
            continue
        if version:
            version_closed = True
        if not names:
            if tok not in _ORGS:
                names.append(tok)
        elif tok in _VARIANTS:
            variants.append(tok)
        elif tok not in _ORGS:
            names.append(tok)
    line = names[0] if names else (variants[0] if variants else base)
    params_b = max(params) if params else None
    family = "-".join(names[:2] + [v for v in variants if v not in names[:2]]) or line
    tier = _size_tier(params_b)
    if tier:
        family += f"@{tier}"
    return ModelIdentity(line=line, family=family, version=tuple(version), stamp=stamp,
                         params_b=params_b, variants=tuple(variants))


def _version_key(ident: ModelIdentity) -> Tuple:
    padded = tuple(ident.version) + (0,) * (4 - len(ident.version))
    return padded[:4] + (ident.stamp,)


def normalize_for_match(model_id: str) -> str:
    """Key used to match a local model id against catalog ids."""
    base = (model_id or "").strip().lower().rsplit("/", 1)[-1]
    base = re.sub(r":(free|beta|extended|thinking|online|nitro|floor)$", "", base)
    base = re.sub(r"[._\s:]+", "-", base)
    base = re.sub(r"-(20\d{2})-?(\d{2})-?(\d{2})$", "", base)
    base = re.sub(r"-(latest|preview|exp)$", "", base)
    return base


# ---------------------------------------------------------------------------
# Public price catalog (OpenRouter format)
# ---------------------------------------------------------------------------

def _cache_path() -> str:
    from src.constants import DATA_DIR

    return os.path.join(DATA_DIR, "model-prices.json")


def _per_mtok(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return round(v * 1_000_000, 4)


def parse_catalog(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Index an OpenRouter ``/api/v1/models`` payload by normalized model id."""
    data = payload.get("data") if isinstance(payload, dict) else None
    index: Dict[str, Dict[str, Any]] = {}
    for item in data or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        pricing = item.get("pricing") or {}
        entry = {
            "catalog_id": item["id"],
            "input_per_mtok": _per_mtok(pricing.get("prompt")),
            "output_per_mtok": _per_mtok(pricing.get("completion")),
            "cached_per_mtok": _per_mtok(pricing.get("input_cache_read")),
            "context_k": round(int(item.get("context_length") or 0) / 1000) or None,
            "created": int(item.get("created") or 0) or None,
            "vision": "image" in str((item.get("architecture") or {}).get("input_modalities") or ""),
        }
        if entry["input_per_mtok"] is None:
            continue
        key = normalize_for_match(item["id"])
        is_free = item["id"].endswith(":free")
        current = index.get(key)
        # Prefer the paid listing: a ":free" variant's zero price says nothing
        # about what the same model costs on a metered endpoint.
        if current is None or (current.get("_free") and not is_free):
            entry["_free"] = is_free
            index[key] = entry
    return index


class PriceCatalog:
    """Daily-cached public list prices; never blocks a request on the network."""

    def __init__(self):
        self._lock = threading.Lock()
        self._index: Optional[Dict[str, Dict[str, Any]]] = None
        self._loaded_at = 0.0
        self._fetched_at = 0.0
        self._refreshing = False
        self.last_error: Optional[str] = None

    @staticmethod
    def enabled() -> bool:
        return os.getenv("ODYSSEUS_MODEL_PRICES", "on").strip().lower() not in ("off", "0", "false", "no")

    @staticmethod
    def url() -> str:
        return os.getenv("ODYSSEUS_MODEL_PRICES_URL", "").strip() or DEFAULT_PRICES_URL

    def _load_cache(self) -> None:
        path = _cache_path()
        try:
            with open(path, encoding="utf-8") as fh:
                cached = json.load(fh)
            self._index = parse_catalog(cached.get("payload"))
            self._fetched_at = float(cached.get("fetched_at") or 0)
        except FileNotFoundError:
            self._index = {}
        except Exception as exc:
            logger.warning("model price cache unreadable: %s", type(exc).__name__)
            self._index = {}
        self._loaded_at = time.time()

    def index(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            if self._index is None:
                self._load_cache()
            stale = time.time() - self._fetched_at > PRICE_CACHE_TTL
            start = stale and self.enabled() and not self._refreshing
            if start:
                self._refreshing = True
            index = self._index or {}
        if start:
            threading.Thread(target=self._refresh_worker, name="model-prices", daemon=True).start()
        return index

    def _refresh_worker(self) -> None:
        try:
            self.refresh()
        except Exception:
            pass
        finally:
            with self._lock:
                self._refreshing = False

    def refresh(self, timeout: float = 20.0) -> int:
        """Fetch the catalog now. Returns the number of priced models."""
        if not self.enabled():
            raise RuntimeError("Public model prices are turned off (ODYSSEUS_MODEL_PRICES=off).")
        import httpx

        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                with client.stream("GET", self.url(), headers={"Accept": "application/json"}) as resp:
                    resp.raise_for_status()
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > _PRICE_FETCH_MAX_BYTES:
                            raise ValueError("price catalog too large")
                        chunks.append(chunk)
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except Exception as exc:
            self.last_error = f"Could not fetch model prices ({type(exc).__name__})."
            with self._lock:
                # Back off for an hour rather than retrying on every request.
                self._fetched_at = time.time() - PRICE_CACHE_TTL + 3600
            raise RuntimeError(self.last_error) from exc
        index = parse_catalog(payload)
        now = time.time()
        try:
            from src.atomic_io import atomic_write_text  # type: ignore
        except Exception:
            atomic_write_text = None
        text = json.dumps({"fetched_at": now, "url": self.url(), "payload": payload})
        try:
            path = _cache_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if atomic_write_text:
                atomic_write_text(path, text)
            else:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
        except Exception as exc:
            logger.warning("model price cache not written: %s", type(exc).__name__)
        with self._lock:
            self._index = index
            self._fetched_at = now
        self.last_error = None
        return len(index)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled(),
                "url": self.url(),
                "models": len(self._index or {}),
                "fetched_at": int(self._fetched_at) or None,
                "error": self.last_error,
            }

    def lookup(self, model_id: str) -> Optional[Dict[str, Any]]:
        index = self.index()
        if not index:
            return None
        key = normalize_for_match(model_id)
        hit = index.get(key)
        if hit is None:
            # Ollama-style tags (qwen3:8b) and -instruct suffixes often differ.
            for alt in (re.sub(r"-instruct$", "", key), key + "-instruct"):
                hit = index.get(alt)
                if hit:
                    break
        return hit


PRICES = PriceCatalog()


# ---------------------------------------------------------------------------
# The roster
# ---------------------------------------------------------------------------

@dataclass
class RosterEntry:
    endpoint_id: str
    endpoint_name: str
    model: str
    kind: str                       # subscription | api | local
    provider: str
    line: str
    family: str
    version: List[int]
    recommended: bool = False
    tier: str = "balanced"          # flagship | balanced | fast
    traits: List[str] = field(default_factory=list)
    params_b: Optional[float] = None
    context_k: Optional[int] = None
    released: Optional[int] = None
    billing: str = "metered"        # metered | subscription | local
    input_per_mtok: Optional[float] = None
    output_per_mtok: Optional[float] = None
    price_source: Optional[str] = None
    notes: Optional[str] = None
    deprecated: bool = False

    @property
    def key(self) -> str:
        return f"{self.endpoint_id}::{self.model}"

    def cost_label(self) -> str:
        if self.billing == "subscription":
            return "subscription (flat; uses plan limits)"
        if self.billing == "local":
            return "free (local hardware)"
        if self.input_per_mtok is None:
            return "price unknown"
        out = f", ${self.output_per_mtok:g} out" if self.output_per_mtok is not None else ""
        approx = "~" if self.price_source == "openrouter" else ""
        return f"{approx}${self.input_per_mtok:g} in{out} per 1M tokens"

    def cost_band(self) -> str:
        """$-$$$$ by blended price (3:1 input:output, typical of agent work)."""
        if self.billing in ("subscription", "local"):
            return ""
        if self.input_per_mtok is None:
            return "?"
        blended = (3 * self.input_per_mtok + (self.output_per_mtok or self.input_per_mtok)) / 4
        if blended < 0.5:
            return "$"
        if blended < 3:
            return "$$"
        if blended < 10:
            return "$$$"
        return "$$$$"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["key"] = self.key
        data["cost_label"] = self.cost_label()
        data["cost_band"] = self.cost_band()
        return data


def _is_local_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if host in {"localhost", "host.docker.internal"} or host.endswith(".local"):
        return True
    # A dotless name is a Docker/Compose service or LAN shortname ("ollama",
    # "gpu-box"); public APIs always use a fully qualified name. Mirrors
    # endpoint_resolver.endpoint_cost_tracked and the chat cost display.
    if host and "." not in host and ":" not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def classify_endpoint(ep) -> Tuple[str, str]:
    """(kind, provider) where kind is subscription | api | local."""
    from src.llm_core import _detect_provider

    base = getattr(ep, "base_url", "") or ""
    try:
        provider = _detect_provider(base)
    except Exception:
        provider = "openai"
    if provider in SUBSCRIPTION_PROVIDERS:
        return "subscription", provider
    # An explicit endpoint kind set in Settings wins over host heuristics.
    declared_kind = str(getattr(ep, "endpoint_kind", "") or "").strip().lower()
    if declared_kind == "local":
        return "local", provider
    if declared_kind in ("api", "proxy"):
        return "api", provider
    try:
        host = urlparse(base).hostname or ""
    except Exception:
        host = ""
    if _is_local_host(host):
        return "local", provider
    return "api", provider


def chat_models(ep) -> List[str]:
    from src.endpoint_resolver import _NON_CHAT_MODEL, _endpoint_enabled_models

    return [m for m in _endpoint_enabled_models(ep)
            if not any(p in m.lower() for p in _NON_CHAT_MODEL)]


def visible_endpoints(db, owner: Optional[str]):
    from core.database import ModelEndpoint
    from src.auth_helpers import owner_filter

    q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
    q = owner_filter(q, ModelEndpoint, owner)
    return [ep for ep in q.all() if (ep.model_type or "llm") == "llm"]


def _tier_for(ident: ModelIdentity, entry: RosterEntry) -> str:
    words = set(ident.variants) | set(ident.family.split("@")[0].split("-"))
    if words & _FAST_WORDS:
        return "fast"
    if words & _FLAGSHIP_WORDS:
        return "flagship"
    if ident.params_b is not None:
        if ident.params_b < 30:
            return "fast"
        if ident.params_b >= 300:
            return "flagship"
    if entry.output_per_mtok is not None:
        if entry.output_per_mtok >= 10:
            return "flagship"
        if entry.output_per_mtok <= 1:
            return "fast"
    return "balanced"


def _traits_for(model_id: str, vision: bool) -> List[str]:
    low = model_id.lower()
    traits = [name for pattern, name in _TRAITS if re.search(pattern, low)]
    if vision and "vision" not in traits:
        traits.append("vision")
    return traits


def mark_recommended(entries: List[RosterEntry], declared: Optional[Dict[str, Any]] = None) -> None:
    """Flag the newest model of each family, skipping previous-generation lines."""
    declared = declared or {}
    idents = {id(e): parse_model_id(e.model) for e in entries}
    line_major: Dict[str, int] = {}
    line_newest: Dict[str, int] = {}
    for e in entries:
        ident = idents[id(e)]
        if ident.version:
            line_major[ident.line] = max(line_major.get(ident.line, 0), ident.version[0])
        if e.released:
            line_newest[ident.line] = max(line_newest.get(ident.line, 0), e.released)
    best: Dict[str, Tuple] = {}
    for e in entries:
        ident = idents[id(e)]
        if not (ident.version or ident.stamp):
            continue
        k = _version_key(ident)
        if ident.family not in best or k > best[ident.family]:
            best[ident.family] = k
    for e in entries:
        ident = idents[id(e)]
        rec = bool(ident.version or ident.stamp) and _version_key(ident) == best.get(ident.family)
        if rec and ident.version and ident.version[0] < line_major.get(ident.line, 0) - 1:
            rec = False                      # a whole generation behind its vendor
        if rec and e.released and line_newest.get(ident.line):
            rec = line_newest[ident.line] - e.released <= _STALE_AFTER_SECONDS
        if e.deprecated:
            rec = False
        override = (declared.get(e.model) or {}).get("recommended") if isinstance(declared.get(e.model), dict) else None
        if isinstance(override, bool):
            rec = override
        e.recommended = rec


def build_entries(raw: Iterable[Tuple[str, str, str, str, str]],
                  declared: Optional[Dict[str, Any]] = None,
                  prices: Optional[PriceCatalog] = None) -> List[RosterEntry]:
    """Roster entries from (endpoint_id, endpoint_name, model, kind, provider) rows."""
    if declared is None:
        from src.omnigent_catalog import load_declared

        declared = load_declared()
    prices = prices or PRICES
    entries: List[RosterEntry] = []
    for endpoint_id, endpoint_name, model, kind, provider in raw:
        ident = parse_model_id(model)
        entry = RosterEntry(endpoint_id=endpoint_id, endpoint_name=endpoint_name, model=model,
                            kind=kind, provider=provider, line=ident.line, family=ident.family,
                            version=list(ident.version), params_b=ident.params_b)
        decl = declared.get(model) if isinstance(declared.get(model), dict) else {}
        vision = False
        if kind == "subscription":
            entry.billing = "subscription"
        elif kind == "local":
            entry.billing = "local"
        if decl.get("input_per_mtok") is not None:
            entry.input_per_mtok = decl.get("input_per_mtok")
            entry.output_per_mtok = decl.get("output_per_mtok")
            entry.price_source = "declared"
        catalog_hit = prices.lookup(model) if prices else None
        if catalog_hit:
            if entry.price_source is None and kind == "api":
                entry.input_per_mtok = catalog_hit.get("input_per_mtok")
                entry.output_per_mtok = catalog_hit.get("output_per_mtok")
                entry.price_source = "openrouter"
            entry.context_k = catalog_hit.get("context_k")
            entry.released = catalog_hit.get("created")
            vision = bool(catalog_hit.get("vision"))
        if decl.get("context_k"):
            entry.context_k = decl.get("context_k")
        entry.notes = decl.get("notes")
        entry.deprecated = bool(decl.get("deprecated") or decl.get("superseded_by"))
        entry.traits = _traits_for(model, vision)
        entry.tier = _tier_for(ident, entry)
        entries.append(entry)
    mark_recommended(entries, declared)
    return entries


def roster(owner: Optional[str]) -> List[RosterEntry]:
    """Every chat model visible to ``owner``, recommended first within each kind."""
    # Through the src.database re-export, the handle the agent tools and their
    # tests use.
    import src.database as dbmod

    rows = []
    db = dbmod.SessionLocal()
    try:
        for ep in visible_endpoints(db, owner):
            kind, provider = classify_endpoint(ep)
            for model in chat_models(ep):
                rows.append((ep.id, ep.name or ep.base_url, model, kind, provider))
    finally:
        db.close()
    entries = build_entries(rows)
    order = {"subscription": 0, "api": 1, "local": 2}
    entries.sort(key=lambda e: (order.get(e.kind, 3), not e.recommended, e.endpoint_name.lower(), e.model))
    return entries


def roster_lines(entries: List[RosterEntry], limit: int = 60) -> str:
    """Compact table for a model's prompt: who is available and what they cost."""
    lines = []
    for e in entries[:limit]:
        bits = [e.kind, e.tier]
        if e.recommended:
            bits.insert(0, "RECOMMENDED")
        if e.traits:
            bits.append("good at " + "/".join(e.traits))
        if e.context_k:
            bits.append(f"{e.context_k}k ctx")
        bits.append(e.cost_label())
        if e.notes:
            bits.append(str(e.notes)[:120])
        lines.append(f"- {e.model} @ {e.endpoint_name} [{e.key}]: " + "; ".join(bits))
    if len(entries) > limit:
        lines.append(f"- ...and {len(entries) - limit} more (list_models shows all)")
    return "\n".join(lines)


ROUTING_GUIDANCE = (
    "Choosing a model: prefer RECOMMENDED models (newest of their family). Match the model "
    "to the task: 'fast' models for lookups, summaries, formatting and other routine work; "
    "'flagship' models for hard reasoning, maths, design and debugging. Prefer subscription "
    "and local models when they can do the job (no per-token charge; subscriptions do count "
    "against plan limits). Among metered models, take the cheapest that can do the job; "
    "where the price is unknown, use the tier as a proxy. Ask a model from a different "
    "vendor than yourself when you want a genuinely independent second opinion."
)
