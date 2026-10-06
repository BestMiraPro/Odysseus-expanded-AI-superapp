#!/usr/bin/env python3
"""Live end-to-end check of the budget ledger against a real model provider.

Starts Odysseus on a throwaway data dir, adds the provider as a metered
endpoint, then makes real calls through every billed path and checks that
each ledger row matches the token usage the provider reported, priced at
the declared list price. Finally it sets a tiny monthly cap and checks that
every path refuses further metered calls.

Spends a few US cents at most (one short reply per path).

Provider (first one configured wins):
  ANTHROPIC_API_KEY             Anthropic API (default model claude-haiku-4-5-20251001)
  GEMINI_API_KEY                Google Gemini, OpenAI-compatible API (default gemini-2.5-flash)
  WANDB_API_KEY                 W&B Inference, OpenAI-compatible API (default meta-llama/Llama-3.1-8B-Instruct);
                                set WANDB_ENTITY + WANDB_PROJECT (or WANDB_PROJECT="entity/project") if
                                your account needs the OpenAI-Project header
  ODYSSEUS_LIVE_BASE_URL +      any OpenAI-compatible API (needs ODYSSEUS_LIVE_MODEL and
  ODYSSEUS_LIVE_API_KEY         ODYSSEUS_LIVE_PRICE_IN / _OUT)
Optional overrides:
  ODYSSEUS_LIVE_MODEL           model id
  ODYSSEUS_LIVE_PRICE_IN/_OUT   USD per 1M input / output tokens used for the expected cost

Usage:  python scripts/budget_live_check.py            (exit code 0 = all checks passed)
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent

# Defaults are list prices per 1M tokens (input, output) at the time of writing.
PROVIDERS = {
    "anthropic": {"env": "ANTHROPIC_API_KEY", "base_url": "https://api.anthropic.com/v1",
                  "model": "claude-haiku-4-5-20251001", "price": (1.0, 5.0)},
    "gemini": {"env": "GEMINI_API_KEY", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-2.5-flash", "price": (0.30, 2.50)},
    # W&B's published price for Llama 3.1 8B at the time of writing; override if it changed.
    "wandb": {"env": "WANDB_API_KEY", "base_url": "https://api.inference.wandb.ai/v1",
              "model": "meta-llama/Llama-3.1-8B-Instruct", "price": (0.22, 0.22)},
}

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)
    return bool(ok)


def pick_provider() -> dict:
    model = os.getenv("ODYSSEUS_LIVE_MODEL")
    p_in, p_out = os.getenv("ODYSSEUS_LIVE_PRICE_IN"), os.getenv("ODYSSEUS_LIVE_PRICE_OUT")
    for name, cfg in PROVIDERS.items():
        key = os.getenv(cfg["env"])
        if key:
            price = (float(p_in), float(p_out)) if p_in and p_out else cfg["price"]
            return {"name": name, "base_url": cfg["base_url"], "key": key, "model": model or cfg["model"],
                    "price": price}
    base, key = os.getenv("ODYSSEUS_LIVE_BASE_URL"), os.getenv("ODYSSEUS_LIVE_API_KEY")
    if base and key and model and p_in and p_out:
        return {"name": "custom", "base_url": base, "key": key, "model": model, "price": (float(p_in), float(p_out))}
    sys.exit("No provider configured: set ANTHROPIC_API_KEY or GEMINI_API_KEY (see the docstring).")


def provider_headers(provider: dict) -> dict:
    if provider["name"] == "anthropic":
        return {"x-api-key": provider["key"], "anthropic-version": "2023-06-01"}
    headers = {"Authorization": f"Bearer {provider['key']}"}
    if provider["name"] == "wandb":
        project = (os.getenv("WANDB_PROJECT") or "").strip()
        entity = (os.getenv("WANDB_ENTITY") or "").strip()
        if project and "/" not in project and entity:
            project = f"{entity}/{project}"
        if project:
            headers["OpenAI-Project"] = project
    return headers


def preflight(provider: dict) -> None:
    """Fail fast, before any spend: blocked host, bad key, or unknown model."""
    from urllib.parse import urlparse

    host = urlparse(provider["base_url"]).hostname
    try:
        r = httpx.get(provider["base_url"].rstrip("/") + "/models", headers=provider_headers(provider), timeout=20)
    except httpx.HTTPError as exc:
        sys.exit(f"Cannot reach {host} ({type(exc).__name__}: {exc}). If this runs in a sandbox, its network "
                 f"policy must allow {host}.")
    if r.status_code in (401, 403):
        sys.exit(f"{host} rejected the key (HTTP {r.status_code}): {r.text[:300]}")
    if r.status_code >= 400:
        print(f"  note: {host}/models answered HTTP {r.status_code}; continuing", flush=True)
        return
    try:
        ids = [m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)]
    except ValueError:
        ids = []
    if ids and provider["model"] not in ids and f"models/{provider['model']}" not in ids:
        sys.exit(f"{provider['model']} is not offered by {host}. Set ODYSSEUS_LIVE_MODEL (and its prices) to one of: "
                 + ", ".join(sorted(ids)[:40]))
    print(f"  preflight ok: {host} reachable, key accepted, {provider['model']} available", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sse_events(text: str):
    for line in text.splitlines():
        if line.startswith("data: ") and not line.startswith("data: [DONE]"):
            try:
                yield json.loads(line[6:])
            except ValueError:
                continue


class Odysseus:
    def __init__(self, provider: dict):
        self.provider = provider
        self.dir = Path(tempfile.mkdtemp(prefix="ody-budget-live-"))
        self.data = self.dir / "data"
        self.data.mkdir()
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.env = {**os.environ, "ODYSSEUS_DATA_DIR": str(self.data), "AUTH_ENABLED": "false",
                    "DATABASE_URL": f"sqlite:///{self.dir / 'app.db'}", "ODYSSEUS_MODEL_PRICES": "off"}
        self.proc = None
        self.http = httpx.Client(base_url=self.base, timeout=180)

    def seed(self) -> str:
        """Declared price, the endpoint, and every Study/research/default route on it."""
        p = self.provider
        (self.data / "omnigent-model-costs.json").write_text(json.dumps(
            {p["model"]: {"input_per_mtok": p["price"][0], "output_per_mtok": p["price"][1]}}))
        ep_id = "liveep01"
        settings = {}
        for role in ("default", "utility", "study", "research"):
            settings[f"{role}_endpoint_id"] = ep_id
            settings[f"{role}_model"] = p["model"]
        (self.data / "settings.json").write_text(json.dumps(settings))
        # The key travels in the environment, never on a command line (visible in `ps`).
        code = (
            "import json, os, core.database as d\n"
            "s = d.SessionLocal()\n"
            f"s.add(d.ModelEndpoint(id={ep_id!r}, name='Live provider', base_url={p['base_url']!r}, owner=None,"
            " is_enabled=True, model_type='llm', endpoint_kind='api', api_key=os.environ['LIVE_SEED_KEY'],"
            f" cached_models=json.dumps([{p['model']!r}])))\n"
            "s.commit()\n"
        )
        subprocess.run([sys.executable, "-c", code], cwd=REPO, env={**self.env, "LIVE_SEED_KEY": p["key"]},
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ep_id

    def start(self) -> None:
        log = open(self.dir / "app.log", "w")
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                                      "--port", str(self.port)], cwd=REPO, env=self.env, stdout=log, stderr=log)
        for _ in range(120):
            try:
                if self.http.get("/api/budget/status").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise SystemExit(f"Odysseus did not start; see {self.dir / 'app.log'}")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def ledger(self) -> dict:
        return self.http.get("/api/budget").json()


def expected_cost(price, usage) -> float:
    read = int(usage.get("cache_read_input_tokens") or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    fresh = max(int(usage.get("input_tokens") or 0) - read - write, 0)
    return (fresh + 0.1 * read + 1.25 * write) * price[0] / 1e6 + int(usage.get("output_tokens") or 0) * price[1] / 1e6


def new_rows(app: Odysseus, before: dict) -> list[dict]:
    time.sleep(1.5)        # turn bookkeeping runs right after the stream ends
    after = app.ledger()
    return after["recent"][: max(after["calls"] - before["calls"], 0)]


def rows_match(name, rows, usages, price, source) -> None:
    got_in = sum(r["input_tokens"] for r in rows)
    got_out = sum(r["output_tokens"] for r in rows)
    want_in = sum(int(u.get("input_tokens") or 0) for u in usages)
    want_out = sum(int(u.get("output_tokens") or 0) for u in usages)
    cost = sum(r["cost_usd"] or 0 for r in rows)
    want_cost = sum(expected_cost(price, u) for u in usages)
    check(f"{name}: billed as '{source}'", rows and all(r["source"] == source for r in rows),
          f"{len(rows)} row(s)")
    check(f"{name}: provider-reported tokens, not estimates", rows and not any(r["estimated"] for r in rows))
    check(f"{name}: tokens match the provider", (got_in, got_out) == (want_in, want_out),
          f"ledger {got_in}/{got_out} vs provider {want_in}/{want_out}")
    check(f"{name}: cost matches list price", abs(cost - want_cost) < 1e-6, f"${cost:.6f} vs ${want_cost:.6f}")


def run() -> int:
    provider = pick_provider()
    print(f"Provider: {provider['name']}  model: {provider['model']}  "
          f"price: ${provider['price'][0]}/${provider['price'][1]} per 1M in/out")
    preflight(provider)
    app = Odysseus(provider)
    ep_id = app.seed()
    app.start()
    seat = {"endpoint_id": ep_id, "model": provider["model"]}
    price = provider["price"]
    try:
        sid = app.http.post("/api/session", data={"name": "budget live", "endpoint_id": ep_id,
                                                   "model": provider["model"]}).json()
        sid = sid.get("id") or sid.get("session_id")

        print("\nChat")
        before = app.ledger()
        r = app.http.post("/api/chat_stream", data={"session": sid, "mode": "chat",
                                                    "message": "Reply with exactly: ok"})
        metrics = next((e["data"] for e in sse_events(r.text) if e.get("type") == "metrics"), {})
        if check("chat: provider reported usage", metrics.get("usage_source") != "estimated" and metrics.get("input_tokens"),
                 f"{metrics.get('input_tokens')}/{metrics.get('output_tokens')} tokens"):
            rows_match("chat", new_rows(app, before), [metrics], price, "chat")

        print("\nAgent")
        before = app.ledger()
        r = app.http.post("/api/chat_stream", data={"session": sid, "mode": "agent",
                                                    "message": "What is 17 times 23? Reply with just the number."})
        metrics = next((e["data"] for e in sse_events(r.text) if e.get("type") == "metrics"), {})
        buckets = metrics.get("usage_buckets") or []
        if check("agent: rounds reported", buckets, f"{len(buckets)} round(s)"):
            rows_match("agent", new_rows(app, before), buckets, price, "agent")

        print("\nCouncil")
        before = app.ledger()
        csid = app.http.post("/api/council/sessions", json={}).json()["id"]
        r = app.http.post(f"/api/council/sessions/{csid}/ask", json={
            "question": "Name one primary colour. One word.", "members": [seat, seat], "chairman": seat,
            "mode": "quick", "budget_confirmed": True})
        done = next((e for e in sse_events(r.text) if e.get("type") == "done"), {})
        turn = app.http.get(f"/api/council/sessions/{csid}").json()["turns"][-1]
        usages = [c["usage"] for c in (turn.get("opinions") or []) if c.get("usage")]
        rows = new_rows(app, before)
        if check("council: turn finished", done.get("status") == "done", f"{len(rows)} call(s)"):
            total = done.get("usage") or {}
            check("council: ledger total equals the turn's total",
                  abs(sum(x["cost_usd"] or 0 for x in rows) - (total.get("cost_usd") or 0)) < 1e-6,
                  f"${sum(x['cost_usd'] or 0 for x in rows):.6f} vs ${total.get('cost_usd') or 0:.6f}")
            check("council: every call billed", len(rows) == len(usages) + 1, f"{len(rows)} rows")

        print("\nStudy")
        before = app.ledger()
        r = app.http.post("/api/study/ai/generate-cards", json={
            "text": "Photosynthesis turns light, water and carbon dioxide into glucose and oxygen.", "count": 1})
        check("study: cards generated", r.status_code == 200, f"HTTP {r.status_code}")
        rows = new_rows(app, before)
        check("study: billed as 'study' with provider usage",
              rows and all(x["source"] == "study" and not x["estimated"] for x in rows), f"{len(rows)} row(s)")
        check("study: priced", rows and all((x["cost_usd"] or 0) > 0 for x in rows))

        print("\nResearch")
        before = app.ledger()
        r = app.http.post("/api/research/start", json={
            "query": "What year did the Eiffel Tower open?", "endpoint_id": ep_id, "model": provider["model"],
            "max_rounds": 1, "max_time": 60})
        rsid = r.json().get("session_id") if r.status_code == 200 else None
        status = {}
        for _ in range(90):
            if not rsid:
                break
            status = app.http.get(f"/api/research/status/{rsid}").json()
            if status.get("status") not in ("running", None):
                break
            time.sleep(2)
        rows = new_rows(app, before)
        check("research: job ran", rsid is not None, f"status {status.get('status')}")
        check("research: billed as 'research' with provider usage",
              rows and all(x["source"] == "research" and not x["estimated"] for x in rows), f"{len(rows)} row(s)")

        print("\nMonthly cap (block)")
        spent = app.ledger()["spent_usd"]
        app.http.put("/api/budget/settings", json={"monthly_cap_usd": max(round(spent / 2, 4), 0.0001),
                                                   "cap_action": "block"})
        before = app.ledger()
        r = app.http.post("/api/chat_stream", data={"session": sid, "mode": "chat", "message": "Reply: ok"})
        check("cap: chat refused", r.status_code == 402, f"HTTP {r.status_code}")
        r = app.http.post(f"/api/council/sessions/{csid}/ask", json={
            "question": "Again?", "members": [seat], "chairman": seat, "mode": "quick", "budget_confirmed": True})
        check("cap: council refused", r.status_code == 402, f"HTTP {r.status_code}")
        r = app.http.post("/api/study/ai/generate-cards", json={"text": "Water boils at 100 C at sea level.", "count": 1})
        check("cap: study refused", r.status_code != 200 and "budget" in r.text.lower(), f"HTTP {r.status_code}")
        r = app.http.post("/api/research/start", json={"query": "Why is the sky blue?", "endpoint_id": ep_id,
                                                       "model": provider["model"], "max_rounds": 1, "max_time": 60})
        rsid = r.json().get("session_id") if r.status_code == 200 else None
        for _ in range(30):
            if not rsid:
                break
            st = app.http.get(f"/api/research/status/{rsid}").json()
            if st.get("status") not in ("running", None):
                break
            time.sleep(1)
        check("cap: no metered call got through", app.ledger()["calls"] == before["calls"],
              f"{app.ledger()['calls'] - before['calls']} new row(s)")
        app.http.put("/api/budget/settings", json={"monthly_cap_usd": 0})

        total = app.ledger()
        print(f"\nTotal spent in this check: ${total['spent_usd']:.4f} over {total['calls']} metered calls")
    finally:
        app.stop()
        if all(ok for _, ok, _ in RESULTS):
            shutil.rmtree(app.dir, ignore_errors=True)
        else:
            print(f"Kept {app.dir} (app.log) for debugging.")
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
