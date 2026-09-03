"""Study route sub-module: ai handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ AI

    @router.post("/ai/generate-cards")
    async def ai_generate_cards(request: Request, body: GenerateCardsIn):
        """Source text -> proposed cards. Returns proposals; nothing is saved."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        text = (body.text or "").strip()
        source = "text"
        if body.material_id:
            picked = _common.card_source_text(user, body.material_id)
            text, source = picked["text"], picked["source"]
        if len(text) < 30:
            raise HTTPException(400, "Provide more source material (at least a paragraph).")
        count = max(1, min(40, body.count))
        focus = f"\nFocus especially on: {body.focus.strip()}" if body.focus else ""
        prompt = (f"Create about {count} flashcards from this material.{focus}\n\n"
                  f"--- MATERIAL ---\n{text[:24000]}")
        value = await _common._llm_json(user, CARD_AUTHOR_SYSTEM, prompt,
                                temperature=0.4, max_tokens=12000,
                                timeout=180, thinking_off=True)
        if isinstance(value, dict):
            value = value.get("cards") or [value]
        if not isinstance(value, list):
            raise HTTPException(502, "Model reply was not a card list. Try again.")
        cards = []
        for item in value:
            if isinstance(item, dict) and item.get("front") and item.get("back"):
                cards.append({"front": str(item["front"]).strip(),
                              "back": str(item["back"]).strip()})
        if not cards:
            raise HTTPException(502, "No usable cards in model reply. Try again.")
        return {"cards": cards, "source": source}
