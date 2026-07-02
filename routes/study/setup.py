"""Assemble the Study APIRouter from the split sub-modules."""

from fastapi import APIRouter


def setup_study_routes() -> APIRouter:
    """Build and return the /api/study router.

    Each sub-module exposes ``register(router)`` which attaches its handlers
    to the shared router.  The order of registration matters only for route
    shadowing — FastAPI matches the first registered path, so more specific
    paths are registered before less specific ones.  The split preserves the
    original handler order so shadowing behaviour is identical.
    """
    router = APIRouter(prefix="/api/study", tags=["study"])

    from routes.study.decks import register as _decks
    from routes.study.cards import register as _cards
    from routes.study.review import register as _review
    from routes.study.ai import register as _ai
    from routes.study.exams import register as _exams
    from routes.study.focus import register as _focus
    from routes.study.materials import register as _materials
    from routes.study.practice import register as _practice
    from routes.study.insights import register as _insights
    from routes.study.maintenance import register as _maintenance

    _decks(router)
    _cards(router)
    _review(router)
    _ai(router)
    _exams(router)
    _focus(router)
    _materials(router)
    _practice(router)
    _insights(router)
    _maintenance(router)

    return router
