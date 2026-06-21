"""Function tools used by the Odysseus Omnigent bridge."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _env() -> tuple[str, str]:
    base_url = (os.environ.get("ODYSSEUS_URL") or "").rstrip("/")
    token = os.environ.get("ODYSSEUS_API_TOKEN") or ""
    if not base_url:
        raise RuntimeError("ODYSSEUS_URL is required")
    if not token:
        raise RuntimeError("ODYSSEUS_API_TOKEN is required")
    return base_url, token


def _request(method: str, path: str, body: dict[str, Any] | None = None, query: dict[str, Any] | None = None) -> Any:
    base_url, token = _env()
    params = {}
    for key, value in (query or {}).items():
        if value is None or value == "":
            continue
        params[key] = str(value)
    url = f"{base_url}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    data = None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Odysseus request failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Odysseus request failed: {exc}") from exc


def capabilities() -> Any:
    return _request("GET", "/api/codex/capabilities")


def todos(
    action: str = "list",
    title: str | None = None,
    text: str | None = None,
    note_id: str | None = None,
    archived: bool = False,
    **extra: Any,
) -> Any:
    clean_action = (action or "list").replace("-", "_").strip().lower()
    if clean_action == "list":
        return _request("GET", "/api/codex/todos", query={"archived": archived, **extra})
    payload = {"action": clean_action, "title": title, "text": text, "note_id": note_id, **extra}
    return _request("POST", "/api/codex/todos", body={k: v for k, v in payload.items() if v is not None})


def email_search(
    folder: str = "INBOX",
    limit: int = 10,
    filter: str = "all",
    account_id: str | None = None,
    **extra: Any,
) -> Any:
    return _request(
        "GET",
        "/api/codex/emails",
        query={"folder": folder, "limit": limit, "filter": filter, "account_id": account_id, **extra},
    )


def memory(action: str = "list", text: str | None = None, category: str = "fact", source: str = "omnigent") -> Any:
    clean_action = (action or "list").replace("-", "_").strip().lower()
    if clean_action == "list":
        return _request("GET", "/api/codex/memory")
    if clean_action == "add":
        return _request("POST", "/api/codex/memory", body={"text": text, "category": category, "source": source})
    raise ValueError(f"Unsupported memory action: {action}")


def calendar_events(start: str, end: str, calendar: str = "") -> Any:
    return _request("GET", "/api/codex/calendar/events", query={"start": start, "end": end, "calendar": calendar})


def documents(search: str | None = None, limit: int = 20) -> Any:
    return _request("GET", "/api/codex/documents", query={"search": search, "limit": limit})


def cookbook_tasks() -> Any:
    return _request("GET", "/api/codex/cookbook/tasks")
