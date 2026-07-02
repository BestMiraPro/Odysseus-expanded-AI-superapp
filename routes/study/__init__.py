"""Study API package (Phase 4.2 split).

The monolithic ``routes/study_routes.py`` has been split into this package.
``routes/study_routes.py`` remains as a one-line shim re-exporting
``setup_study_routes`` so existing imports (``app.py``) keep working.
"""

from routes.study.setup import setup_study_routes  # noqa: F401

__all__ = ["setup_study_routes"]
