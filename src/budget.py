"""Spend ledger and monthly budget guardrails for metered models.

Only metered (pay-per-token API) calls cost money per call. Subscription
models (Claude / ChatGPT plans) and local models are never recorded, priced
or blocked here.

Three guardrails, all per user:

* ``monthly_cap_usd`` - a calendar-month (UTC) ceiling on metered spend.
  With ``cap_action == "block"`` a metered call that would cross it is
  refused; with ``"warn"`` it runs and the UI shows the overrun. 0 = no cap.
* ``action_limit_usd`` - the most one action may cost before a person
  confirms it: a Council turn asks first, an agent delegation is refused with
  cheaper alternatives (the agent cannot approve its own spend). 0 = no limit.
* a warning from :data:`WARN_FRACTION` of the cap.

Prices come from the shared model roster (declared costs, then the public
price catalog), so the ledger, the pickers and these checks agree on what a
model costs. A model with no known price is recorded with ``cost_usd=None``
and counted as "unpriced" rather than guessed.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

WARN_FRACTION = 0.8
CAP_ACTIONS = ("block", "warn")
# The default single-action limit is high enough for ordinary delegations
# (tens of thousands of tokens to a flagship model) and still stops a runaway.
DEFAULTS: Dict[str, Any] = {"monthly_cap_usd": 0.0, "cap_action": "block", "action_limit_usd": 1.0}
MAX_USD = 1_000_000.0

# Typical reply lengths (tokens) used when an estimate has to guess the output
# side. Council estimates prefer the user's own recent averages when known.
EXPECTED_OUTPUT_TOKENS = {"opinions": 900, "review": 600, "synthesis": 1200, "delegation": 800}

# Cached prompt tokens bill differently from fresh input (Anthropic: reads
# ~0.1x, 5-minute writes 1.25x); they are already counted inside input_tokens.
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


def _session():
    import core.database as cdb

    return cdb.SessionLocal()


def _owner_key(owner: Optional[str]) -> str:
    return owner or ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def money(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value < 0.01:
        return f"${value:.4f}" if value else "$0"
    return f"${value:,.2f}"


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Price:
    billing: str = "metered"            # metered | subscription | local
    input_per_mtok: Optional[float] = None
    output_per_mtok: Optional[float] = None
    source: Optional[str] = None

    @property
    def metered(self) -> bool:
        return self.billing == "metered"

    @property
    def known(self) -> bool:
        return self.metered and self.input_per_mtok is not None

    def cost(self, input_tokens: int = 0, output_tokens: int = 0,
             cache_read: int = 0, cache_write: int = 0) -> Optional[float]:
        """USD for one call, or None when free, flat-rate or unpriced."""
        if not self.known:
            return None
        out_price = self.output_per_mtok if self.output_per_mtok is not None else self.input_per_mtok
        fresh = max(int(input_tokens or 0) - int(cache_read or 0) - int(cache_write or 0), 0)
        input_cost = (fresh + CACHE_READ_FACTOR * int(cache_read or 0)
                      + CACHE_WRITE_FACTOR * int(cache_write or 0)) * self.input_per_mtok
        return round((input_cost + int(output_tokens or 0) * out_price) / 1_000_000, 6)

    def usage_cost(self, usage: Optional[Dict[str, Any]]) -> Optional[float]:
        if not usage:
            return None
        return self.cost(usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
                         usage.get("cache_read_input_tokens") or 0,
                         usage.get("cache_creation_input_tokens") or 0)


_PRICE_CACHE: Dict[Tuple[str, str, str], Tuple[float, Price]] = {}
_PRICE_TTL = 300.0
_PRICE_LOCK = threading.Lock()


def price_for(base_url: str, model: str, endpoint_kind: Optional[str] = None) -> Price:
    """Billing and list price of ``model`` served from ``base_url``.

    ``endpoint_kind`` is the endpoint's saved kind ("local", "api", ...); an
    explicit "local" makes it free whatever the host looks like.
    """
    key = (base_url or "", model or "", (endpoint_kind or "").lower())
    now = time.monotonic()
    with _PRICE_LOCK:
        hit = _PRICE_CACHE.get(key)
        if hit and now - hit[0] < _PRICE_TTL:
            return hit[1]
    try:
        from src import model_roster

        kind, provider = model_roster.classify_endpoint(
            SimpleNamespace(base_url=base_url or "", endpoint_kind=endpoint_kind))
        (entry,) = model_roster.build_entries([("", "", model or "", kind, provider)])
        price = Price(entry.billing, entry.input_per_mtok, entry.output_per_mtok, entry.price_source)
    except Exception as exc:
        logger.debug("budget: pricing failed for %s: %s", model, exc)
        price = Price()
    with _PRICE_LOCK:
        if len(_PRICE_CACHE) > 512:
            _PRICE_CACHE.clear()
        _PRICE_CACHE[key] = (now, price)
    return price


def clear_price_cache() -> None:
    with _PRICE_LOCK:
        _PRICE_CACHE.clear()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_settings(owner: Optional[str]) -> Dict[str, Any]:
    from core.database import BudgetSetting

    db = _session()
    try:
        row = db.query(BudgetSetting).filter(BudgetSetting.owner_key == _owner_key(owner)).first()
        if row is None:
            return dict(DEFAULTS)
        return {"monthly_cap_usd": float(row.monthly_cap_usd or 0.0),
                "cap_action": row.cap_action if row.cap_action in CAP_ACTIONS else "block",
                "action_limit_usd": float(row.action_limit_usd or 0.0)}
    finally:
        db.close()


def _clean_amount(value: Any, name: str) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if amount != amount or amount < 0 or amount > MAX_USD:   # NaN, negative, absurd
        raise ValueError(f"{name} must be between 0 and {MAX_USD:,.0f}")
    return round(amount, 4)


def save_settings(owner: Optional[str], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and store a partial update; returns the full settings."""
    from core.database import BudgetSetting

    current = get_settings(owner)
    if "monthly_cap_usd" in patch and patch["monthly_cap_usd"] is not None:
        current["monthly_cap_usd"] = _clean_amount(patch["monthly_cap_usd"], "Monthly cap")
    if "action_limit_usd" in patch and patch["action_limit_usd"] is not None:
        current["action_limit_usd"] = _clean_amount(patch["action_limit_usd"], "Single-action limit")
    if "cap_action" in patch and patch["cap_action"] is not None:
        if patch["cap_action"] not in CAP_ACTIONS:
            raise ValueError("cap_action must be 'block' or 'warn'")
        current["cap_action"] = patch["cap_action"]
    db = _session()
    try:
        row = db.query(BudgetSetting).filter(BudgetSetting.owner_key == _owner_key(owner)).first()
        if row is None:
            row = BudgetSetting(owner_key=_owner_key(owner))
            db.add(row)
        row.monthly_cap_usd = current["monthly_cap_usd"]
        row.cap_action = current["cap_action"]
        row.action_limit_usd = current["action_limit_usd"]
        db.commit()
    finally:
        db.close()
    return current


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def record(
    owner: Optional[str],
    *,
    source: str,
    model: str,
    usage: Optional[Dict[str, Any]] = None,
    price: Optional[Price] = None,
    base_url: Optional[str] = None,
    endpoint_id: Optional[str] = None,
    endpoint_name: Optional[str] = None,
    session_id: Optional[str] = None,
    cost_usd: Optional[float] = None,
    estimated: bool = False,
) -> Optional[float]:
    """Add one metered call to the ledger; returns its cost (None if unpriced).

    Non-metered calls and calls with no tokens are ignored. Never raises: a
    ledger failure must not break the call it accounts for.
    """
    try:
        if price is None:
            price = price_for(base_url or "", model)
        if not price.metered:
            return None
        usage = usage or {}
        tokens_in = max(int(usage.get("input_tokens") or 0), 0)
        tokens_out = max(int(usage.get("output_tokens") or 0), 0)
        if not tokens_in and not tokens_out:
            return None
        cache_read = max(int(usage.get("cache_read_input_tokens") or 0), 0)
        cache_write = max(int(usage.get("cache_creation_input_tokens") or 0), 0)
        if cost_usd is None:
            cost_usd = price.cost(tokens_in, tokens_out, cache_read, cache_write)
        from core.database import SpendEntry

        db = _session()
        try:
            db.add(SpendEntry(
                owner=owner or None, source=(source or "chat")[:32], session_id=session_id,
                endpoint_id=endpoint_id, endpoint_name=(endpoint_name or None),
                model=(model or "")[:300], input_tokens=tokens_in, output_tokens=tokens_out,
                cache_read_tokens=cache_read, cache_write_tokens=cache_write,
                cost_usd=cost_usd, estimated=bool(estimated),
            ))
            db.commit()
        finally:
            db.close()
        return cost_usd
    except Exception as exc:
        logger.warning("budget: could not record spend for %s: %s", model, exc)
        return None


def _endpoints(endpoint_ids: Iterable[str]) -> Dict[str, Tuple[str, str, Optional[str]]]:
    """{endpoint_id: (base_url, name, endpoint_kind)} for pricing saved routes."""
    ids = [i for i in {*endpoint_ids} if i]
    if not ids:
        return {}
    from core.database import ModelEndpoint

    db = _session()
    try:
        return {ep.id: (ep.base_url or "", ep.name or "", getattr(ep, "endpoint_kind", None))
                for ep in db.query(ModelEndpoint).filter(ModelEndpoint.id.in_(ids)).all()}
    finally:
        db.close()


def endpoint_for_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(endpoint_kind, name, id) of the saved endpoint serving ``url`` (a base or chat URL)."""
    from core.database import ModelEndpoint

    norm = (url or "").rstrip("/")
    if not norm:
        return None, None, None
    try:
        db = _session()
        try:
            best = None
            for ep in db.query(ModelEndpoint).all():
                base = (ep.base_url or "").rstrip("/")
                if base and (norm == base or norm.startswith(base + "/")):
                    if best is None or len(base) > len((best.base_url or "").rstrip("/")):
                        best = ep
            if best is not None:
                return getattr(best, "endpoint_kind", None), best.name or None, best.id
        finally:
            db.close()
    except Exception as exc:
        logger.debug("budget: endpoint lookup failed: %s", exc)
    return None, None, None


def endpoint_kind_for_url(url: str) -> Optional[str]:
    """The saved endpoint_kind of the endpoint serving ``url`` (a base or chat URL)."""
    return endpoint_for_url(url)[0]


def record_bucket(owner: Optional[str], session_id: Optional[str], bucket: Dict[str, Any], *,
                  source: str = "agent", base_url: str = "") -> Optional[float]:
    """Bill one agent model call (a usage bucket) as soon as it ends.

    The agent loop calls this per round, so a turn that is stopped midway is
    still billed and checks later in the same turn see the spend.
    """
    try:
        if not isinstance(bucket, dict) or bucket.get("endpoint_cost_tracked") is False:
            return None
        endpoint_id = bucket.get("endpoint_id") or None
        url, name, kind = _endpoints([endpoint_id or ""]).get(endpoint_id or "", (base_url, "", None))
        model = bucket.get("model") or ""
        return record(owner, source=source, model=model, usage=bucket,
                      price=price_for(url, model, kind), endpoint_id=endpoint_id,
                      endpoint_name=bucket.get("endpoint_label") or name or None, session_id=session_id,
                      estimated=bucket.get("usage_source") == "estimated")
    except Exception as exc:
        logger.warning("budget: agent round not recorded: %s", exc)
        return None


def record_turn(owner: Optional[str], session_id: Optional[str], metrics: Dict[str, Any], *,
                base_url: str = "") -> float:
    """Bill one saved plain-chat turn from its metrics; returns its cost.

    Agent turns (metrics with ``usage_buckets``) are skipped: the agent loop
    bills each round as it ends (:func:`record_bucket`), which also covers
    stopped turns and inline teacher runs.
    """
    if not isinstance(metrics, dict) or metrics.get("usage_buckets") or metrics.get("spend_recorded"):
        return 0.0
    if metrics.get("endpoint_cost_tracked") is False:
        return 0.0
    model = metrics.get("model") or ""
    endpoint_id = metrics.get("endpoint_id") or None
    try:
        url, name, kind = _endpoints([endpoint_id or ""]).get(endpoint_id or "", (base_url, "", None))
        if kind is None:
            kind = endpoint_kind_for_url(url)
    except Exception as exc:
        logger.debug("budget: endpoint lookup failed: %s", exc)
        url, name, kind = base_url, "", None
    cost = record(owner, source="chat", model=model, usage=metrics, price=price_for(url, model, kind),
                  endpoint_id=endpoint_id, endpoint_name=metrics.get("endpoint_label") or name or None,
                  session_id=session_id, estimated=metrics.get("usage_source") == "estimated")
    return round(cost or 0.0, 6)


def month_window(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """[first day of this UTC month, first day of the next) as naive UTC."""
    now = now or _utcnow()
    start = datetime(now.year, now.month, 1)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
    return start, end


def _month_rows(db, owner: Optional[str], now: Optional[datetime] = None):
    from core.database import SpendEntry

    start, end = month_window(now)
    return db.query(SpendEntry).filter(SpendEntry.owner == (owner or None),
                                       SpendEntry.created_at >= start, SpendEntry.created_at < end)


def month_spend(owner: Optional[str], now: Optional[datetime] = None) -> float:
    from sqlalchemy import func

    from core.database import SpendEntry

    db = _session()
    try:
        start, end = month_window(now)
        total = (db.query(func.coalesce(func.sum(SpendEntry.cost_usd), 0.0))
                 .filter(SpendEntry.owner == (owner or None),
                         SpendEntry.created_at >= start, SpendEntry.created_at < end)
                 .scalar())
        return round(float(total or 0.0), 6)
    finally:
        db.close()


def _state(spent: float, settings: Dict[str, Any]) -> str:
    cap = settings.get("monthly_cap_usd") or 0.0
    if cap <= 0:
        return "ok"
    if spent >= cap:
        return "over"
    if spent >= cap * WARN_FRACTION:
        return "warn"
    return "ok"


def status(owner: Optional[str], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Small payload for banners: spend so far this month against the cap."""
    settings = get_settings(owner)
    spent = month_spend(owner, now)
    cap = settings["monthly_cap_usd"]
    return {
        **settings,
        "spent_usd": spent,
        "remaining_usd": round(max(cap - spent, 0.0), 6) if cap > 0 else None,
        "fraction": round(spent / cap, 4) if cap > 0 else None,
        "state": _state(spent, settings),
        "warn_fraction": WARN_FRACTION,
    }


def summary(owner: Optional[str], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Month-to-date spend with a breakdown, a projection and recent calls."""
    now = now or _utcnow()
    out = status(owner, now)
    db = _session()
    try:
        rows = _month_rows(db, owner, now).all()
    finally:
        db.close()
    by_source: Dict[str, Dict[str, Any]] = {}
    by_model: Dict[str, Dict[str, Any]] = {}
    unpriced = 0
    tokens_in = tokens_out = 0
    for row in rows:
        tokens_in += row.input_tokens or 0
        tokens_out += row.output_tokens or 0
        if row.cost_usd is None:
            unpriced += 1
        for bucket, key in ((by_source, row.source or "other"), (by_model, row.model or "?")):
            agg = bucket.setdefault(key, {"cost_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0})
            agg["calls"] += 1
            agg["cost_usd"] += float(row.cost_usd or 0.0)
            agg["input_tokens"] += row.input_tokens or 0
            agg["output_tokens"] += row.output_tokens or 0
            if key == (row.model or "?") and bucket is by_model and row.endpoint_name:
                agg["endpoint_name"] = row.endpoint_name

    def _sorted(bucket):
        items = [{"name": k, **{kk: (round(vv, 6) if isinstance(vv, float) else vv) for kk, vv in v.items()}}
                 for k, v in bucket.items()]
        return sorted(items, key=lambda r: (-r["cost_usd"], -r["calls"], r["name"]))

    start, end = month_window(now)
    days_in_month = (end - start).days
    elapsed_days = max((now - start).total_seconds() / 86400.0, 1.0)
    projected = out["spent_usd"] / elapsed_days * days_in_month if out["spent_usd"] else 0.0
    recent = sorted(rows, key=lambda r: r.created_at, reverse=True)[:25]
    out.update({
        "month": start.strftime("%Y-%m"),
        "month_start": start.isoformat() + "Z",
        "month_end": end.isoformat() + "Z",
        "calls": len(rows),
        "unpriced_calls": unpriced,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "projected_usd": round(projected, 4),
        "by_source": _sorted(by_source),
        "by_model": _sorted(by_model)[:12],
        "recent": [{
            "at": r.created_at.isoformat() + "Z", "source": r.source, "model": r.model,
            "endpoint_name": r.endpoint_name, "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens, "cost_usd": r.cost_usd, "estimated": bool(r.estimated),
        } for r in recent],
    })
    return out


# ---------------------------------------------------------------------------
# Guardrail decisions
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    allowed: bool = True
    confirm: bool = False
    reason: str = ""
    estimate_usd: Optional[float] = None
    spent_usd: float = 0.0
    settings: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        cap = self.settings.get("monthly_cap_usd") or 0.0
        return {"allowed": self.allowed, "confirm": self.confirm, "reason": self.reason,
                "estimate_usd": self.estimate_usd, "spent_usd": self.spent_usd,
                "monthly_cap_usd": cap, "cap_action": self.settings.get("cap_action"),
                "action_limit_usd": self.settings.get("action_limit_usd") or 0.0,
                "state": _state(self.spent_usd, self.settings)}


def check(owner: Optional[str], estimate_usd: Optional[float], *, metered: bool = True,
          what: str = "This action") -> Decision:
    """Whether a metered action of ``estimate_usd`` may run now.

    ``allowed=False``: the monthly cap (block mode) stops it; nothing can
    override that except raising the cap. ``confirm=True``: it may run once a
    person agrees, because it costs more than the single-action limit.
    """
    try:
        settings = get_settings(owner)
        if not metered:
            return Decision(settings=settings)
        # Without a cap the month total only feeds a display: skip the SUM.
        spent = month_spend(owner) if settings["monthly_cap_usd"] > 0 else 0.0
    except Exception as exc:
        # A broken budget store must not stop every model call: fail open.
        logger.warning("budget: check skipped, store unavailable: %s", exc)
        return Decision(estimate_usd=estimate_usd, settings=dict(DEFAULTS, monthly_cap_usd=0.0,
                                                                  action_limit_usd=0.0))
    decision = Decision(estimate_usd=estimate_usd, spent_usd=spent, settings=settings)
    cap = settings["monthly_cap_usd"]
    if cap > 0 and settings["cap_action"] == "block":
        if spent >= cap:
            decision.allowed = False
            decision.reason = (f"Monthly budget reached: {money(spent)} of {money(cap)} spent on metered "
                               "models this month. Use a subscription or local model, or raise the cap "
                               "in Settings → Budget.")
            return decision
        if estimate_usd is not None and spent + estimate_usd > cap:
            decision.allowed = False
            decision.reason = (f"{what} (≈{money(estimate_usd)}) would take this month's metered spend past "
                               f"your {money(cap)} cap ({money(spent)} spent). Pick cheaper or local "
                               "models, or raise the cap in Settings → Budget.")
            return decision
    limit = settings["action_limit_usd"]
    if limit > 0 and estimate_usd is not None and estimate_usd > limit:
        decision.confirm = True
        decision.reason = (f"{what} is estimated at ≈{money(estimate_usd)}, above your "
                           f"{money(limit)} single-action limit.")
    return decision


def cap_block_reason(owner: Optional[str]) -> Optional[str]:
    """Why metered calls are refused right now, whatever the model (cap reached, block mode)."""
    try:
        settings = get_settings(owner)
        if settings["monthly_cap_usd"] <= 0 or settings["cap_action"] != "block":
            return None
        decision = check(owner, None, what="This call")
        return None if decision.allowed else decision.reason
    except Exception as exc:
        logger.warning("budget: cap check failed: %s", exc)
        return None


def chat_block_reason(owner: Optional[str], base_url: str, model: str,
                      endpoint_kind: Optional[str] = None) -> Optional[str]:
    """Why a metered model call must not start (cap reached in block mode)."""
    try:
        settings = get_settings(owner)
        if settings["monthly_cap_usd"] <= 0 or settings["cap_action"] != "block":
            return None          # the common case: one small query, no pricing
        if endpoint_kind is None:
            endpoint_kind = endpoint_kind_for_url(base_url)
        price = price_for(base_url, model, endpoint_kind)
        if not price.metered:
            return None
        decision = check(owner, None, what="This message")
        return None if decision.allowed else decision.reason
    except Exception as exc:
        logger.warning("budget: chat check failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------

def estimate_tokens(messages: Iterable[Dict[str, Any]]) -> int:
    try:
        from src.model_context import estimate_tokens as _estimate

        return int(_estimate(list(messages)))
    except Exception:
        return sum(len(str(m.get("content") or "")) // 3 + 4 for m in messages)


def estimate_call(price: Price, input_tokens: int, output_tokens: int) -> Optional[float]:
    return price.cost(input_tokens, output_tokens)


def text_tokens(text: str) -> int:
    return estimate_tokens([{"role": "user", "content": text or ""}])


# ---------------------------------------------------------------------------
# Metering scopes: research, Study and other work that calls llm_core directly
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MeterScope:
    owner: Optional[str]
    source: str
    session_id: Optional[str] = None


_SCOPE: "contextvars.ContextVar[Optional[MeterScope]]" = contextvars.ContextVar("odysseus_budget_scope",
                                                                               default=None)


def current_scope() -> Optional[MeterScope]:
    return _SCOPE.get()


@contextlib.contextmanager
def metering(owner: Optional[str], source: str, session_id: Optional[str] = None):
    """Bill every llm_core call made inside this block to ``owner`` as ``source``.

    ``llm_call_async`` and ``stream_llm`` record their usage (provider-reported,
    else estimated) and refuse metered calls once a blocking cap is reached.
    Chat, agent rounds, Council and delegations bill themselves and run
    outside any scope, so nothing is counted twice.
    """
    previous = _SCOPE.get()
    token = _SCOPE.set(MeterScope(owner or None, source, session_id))
    try:
        yield
    finally:
        try:
            _SCOPE.reset(token)
        except ValueError:          # finalised in another context (async generator cleanup)
            _SCOPE.set(previous)


def metered(source: str):
    """Decorator for ``async def fn(owner, ...)``: run it inside :func:`metering`."""
    import functools

    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(owner, *args, **kwargs):
            with metering(owner, source):
                return await fn(owner, *args, **kwargs)
        return wrapper
    return decorate


async def metered_stream(agen, owner: Optional[str], source: str, session_id: Optional[str] = None):
    """Iterate a ``stream_llm`` generator inside a metering scope.

    The scope is active only while a chunk is being awaited, never while the
    caller (often itself a generator) is suspended, so it cannot leak into
    unrelated calls made by the consumer.
    """
    try:
        while True:
            with metering(owner, source, session_id):
                try:
                    chunk = await agen.__anext__()
                except StopAsyncIteration:
                    return
            yield chunk
    finally:
        await agen.aclose()


def scope_block_reason(scope: Optional[MeterScope], url: str, model: str) -> Optional[str]:
    """Why a scoped metered call must not start (cap reached, block mode)."""
    if scope is None:
        return None
    return chat_block_reason(scope.owner, url, model)


def usage_from_response(provider: str, data: Any) -> Optional[Dict[str, int]]:
    """Token usage from a non-streaming provider response, or None."""
    if not isinstance(data, dict):
        return None
    try:
        if provider == "ollama":
            tin, tout = int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
            return {"input_tokens": tin, "output_tokens": tout} if (tin or tout) else None
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None
        if provider == "anthropic":
            read = int(usage.get("cache_read_input_tokens") or 0)
            write = int(usage.get("cache_creation_input_tokens") or 0)
            out = {"input_tokens": int(usage.get("input_tokens") or 0) + read + write,
                   "output_tokens": int(usage.get("output_tokens") or 0)}
            if read:
                out["cache_read_input_tokens"] = read
            if write:
                out["cache_creation_input_tokens"] = write
            return out
        tin = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        tout = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        return {"input_tokens": tin, "output_tokens": tout} if (tin or tout) else None
    except (TypeError, ValueError):
        return None


def record_scoped(scope: Optional[MeterScope], url: str, model: str, usage: Optional[Dict[str, Any]],
                  messages: Optional[List[Dict[str, Any]]] = None, text: str = "") -> Optional[float]:
    """Bill one scoped call: provider usage when given, else an estimate."""
    if scope is None:
        return None
    try:
        kind, name, endpoint_id = endpoint_for_url(url)
        price = price_for(url, model, kind)
        if not price.metered:
            return None
        estimated = not usage
        if estimated:
            if not text and not messages:
                return None
            usage = {"input_tokens": estimate_tokens(messages or []),
                     "output_tokens": text_tokens(text) if text else 0}
        return record(scope.owner, source=scope.source, model=model, usage=usage, price=price,
                      endpoint_id=endpoint_id, endpoint_name=name, session_id=scope.session_id,
                      estimated=estimated)
    except Exception as exc:
        logger.warning("budget: %s call not recorded: %s", getattr(scope, "source", "?"), exc)
        return None
