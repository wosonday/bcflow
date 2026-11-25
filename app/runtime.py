"""UI state and the one shared local-workspace job manager."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import streamlit as st

from app.services.config import config_digest, resolve_document
from app.services.runner import JobManager


@st.cache_resource(max_entries=1)
def get_manager() -> JobManager:
    """All tabs on this personal localhost app share one active backend process."""
    return JobManager(Path.cwd(), cwd=Path.cwd())


def init_session() -> None:
    defaults = {
        "document": None,
        "editor_buffer": "",
        "config_path": "configs/example_toy.yaml",
        "tracked_job_id": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _json_date(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported config value: {type(value).__name__}")


def current_config() -> tuple[dict | None, str | None]:
    """Parse the editor, preserving its original base directory across snapshots."""
    document = st.session_state.get("document")
    if document is None:
        return None, None
    try:
        resolved = resolve_document(
            st.session_state.get("editor_buffer", document["text"]),
            document["format"],
            document["base_dir"],
        )
        # YAML dates are accepted by the backend but need ISO text for JSON IPC.
        normalized = json.loads(
            json.dumps(resolved, default=_json_date, allow_nan=False)
        )
        config_digest(normalized)
        return normalized, None
    except (ValueError, TypeError, OSError, RecursionError) as exc:
        return None, str(exc)


def active_job() -> dict | None:
    return next((job for job in get_manager().history() if job["active"]), None)


def matching_validation(digest: str) -> dict | None:
    return next(
        (
            job
            for job in get_manager().history()
            if job["kind"] == "validate"
            and job["config_digest"] == digest
            and not job["active"]
        ),
        None,
    )
