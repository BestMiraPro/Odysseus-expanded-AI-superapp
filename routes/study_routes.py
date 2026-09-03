# routes/study_routes.py
"""Backward-compatibility shim.

The Study API has been split into the ``routes/study/`` package (Phase 4.2).
This module re-exports the public surface that ``app.py`` and the test suite
import from ``routes.study_routes`` so no call site needs to change.

Tests commonly do ``monkeypatch.setattr(study_routes, "_read_pref", fake)``
to stub out helper functions.  After the split the handlers live in the
``routes.study.*`` sub-modules and resolve those names via the shared
``routes.study._common`` module.  For the monkeypatches to take effect they
must reach *that* module, so this shim transparently forwards every
attribute **write** to ``routes.study._common`` while still exposing all of
its names for **reads**.
"""

import sys
import types

from routes.study._common import *  # noqa: F401,F403
from routes.study._common import (  # noqa: F401  (explicit for underscore names)
    AttemptIn,
    RateLimiter,
    _optimize_locks,
    _optimize_status,
    _read_pref,
    _resolve_uploaded_file,
    card_source_text,
    _same_or_later_study_part,
    _same_study_question,
    fsrs,
)
from routes.study.setup import setup_study_routes  # noqa: F401

# Service functions the Study agent (src/study_agent.py) calls through this
# module. They live in the sub-modules after the split; re-exported here so the
# agent keeps one import surface.
from routes.study.practice import (  # noqa: F401
    _round_robin,
    _split_topics,
    practice_queue_payload,
)
from routes.study.materials import (  # noqa: F401
    create_material_record,
    material_rows_with_counts,
    run_extraction,
    run_transcribe_material,
)
from routes.study.insights import (  # noqa: F401
    history_entries,
    overview_payload,
)
from routes.study.maintenance import (  # noqa: F401
    run_audit_questions,
    run_backfill_context,
    run_dedup,
    run_reformat,
)

_common = sys.modules["routes.study._common"]


class _ForwardingModule(types.ModuleType):
    """Module subclass that forwards writes to the ``_common`` module."""

    def __setattr__(self, name, value):
        setattr(_common, name, value)
        # Also set locally so ``hasattr`` / ``getattr`` on the shim reflects it
        # immediately even before ``_common`` is re-imported by a sub-module.
        super().__setattr__(name, value)


# Replace this module's class so attribute writes are forwarded.
sys.modules[__name__].__class__ = _ForwardingModule
