"""UI state and the one shared local-workspace job manager."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from app.services.config import normalize_config_for_json, resolve_document
from app.services.runner import JobManager


class SessionKeys:
    """Session_state and widget keys shared across multiple sites."""

    DOCUMENT = "document"
    EDITOR_BUFFER = "editor_buffer"
    CONFIG_PATH = "config_path"
    TRACKED_JOB_ID = "tracked_job_id"
    EXAMPLE_CONFIG = "example_config"
    CONFIG_EDITOR = "config_editor"
    MONITOR_TRACKING = "_monitor_tracking"
    JOB_CHOICE = "job_choice"


@st.cache_resource(max_entries=1)
def get_manager() -> JobManager:
    """All tabs on this personal localhost app share one active backend process."""
    return JobManager(Path.cwd(), cwd=Path.cwd())


def init_session() -> None:
    for key, value in {
        SessionKeys.DOCUMENT: None,
        SessionKeys.EDITOR_BUFFER: "",
        SessionKeys.CONFIG_PATH: "configs/example_toy.yaml",
        SessionKeys.TRACKED_JOB_ID: None,
    }.items():
        st.session_state.setdefault(key, value)


def current_config_with_digest() -> tuple[dict | None, str, str | None]:
    """Parse the editor, returning ``(normalized_config, digest, error)``."""
    document = st.session_state.get(SessionKeys.DOCUMENT)
    if document is None:
        return None, "", None
    try:
        resolved = resolve_document(
            st.session_state.get(SessionKeys.EDITOR_BUFFER, document["text"]),
            document["format"],
            document["base_dir"],
        )
        # YAML dates are accepted by the backend but need ISO text for JSON IPC.
        normalized, digest = normalize_config_for_json(resolved)
        return normalized, digest, None
    except (ValueError, TypeError, OSError, RecursionError) as exc:
        return None, "", str(exc)


def current_config() -> tuple[dict | None, str | None]:
    """Parse the editor, preserving its original base directory across snapshots."""
    config, _digest, error = current_config_with_digest()
    return config, error


def active_job() -> dict | None:
    return next((job for job in get_manager().history() if job["active"]), None)


def matching_validation(digest: str) -> dict | None:
    return next(
        (
            job
            for job in get_manager().history()
            if job["kind"] == "validate" and job["config_digest"] == digest and not job["active"]
        ),
        None,
    )
