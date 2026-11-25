"""Shared native Streamlit presentation helpers for the app views."""

from __future__ import annotations

from typing import Literal, get_args

import pandas as pd
import streamlit as st

from app.services._io import MAX_JSON_BYTES, dump_json_strict
from app.services.results import qa_checks_frame
from bcflow.qa import Status

_QA_STATUSES = tuple(get_args(Status))
_QA_METRIC_LABELS = {"pass": "Passed", "warn": "Warnings", "fail": "Failed"}


def show_warning(message: str, exc: Exception | None = None) -> None:
    """Display a warning banner, truncating long exception text."""
    text = f"{message}: {str(exc)[:500]}" if exc is not None else message
    st.warning(text, icon=":material/warning:")


def render_validation_messages(summary: dict) -> None:
    """Render validation errors and warnings from a backend summary payload."""
    for item in summary.get("errors", []):
        location = ".".join(map(str, item.get("loc", [])))
        st.error(f"{location}: {item.get('message', '')}")
    for item in summary.get("warnings", []):
        st.warning(item.get("message", str(item)))


def render_variable_status_table(statuses: dict[str, str], *, alt: str) -> None:
    """Render a two-column Variable/Status table."""
    table = pd.DataFrame(
        [{"Variable": name, "Status": status} for name, status in statuses.items()],
        columns=["Variable", "Status"],
    )
    st.dataframe(table, hide_index=True, width="stretch", alt=alt)


def render_qa_panel(checks: list[dict], *, alt: str) -> None:
    """Render QA check metrics, status/variable filters, and the normalized table."""
    table = qa_checks_frame(checks)
    with st.container(horizontal=True):
        st.metric("Total checks", len(checks))
        for status in _QA_STATUSES:
            label = _QA_METRIC_LABELS.get(status, status.capitalize())
            st.metric(label, int((table["Status"] == status).sum()))
    statuses = list(dict.fromkeys([*_QA_STATUSES, *table["Status"].tolist()]))
    selected = st.multiselect("QA status", statuses, default=statuses, key="results_qa_status")
    variables = list(dict.fromkeys(table["Variable"].tolist()))
    variable = st.selectbox(
        "QA variable",
        [None, *variables],
        format_func=lambda item: "All variables" if item is None else item,
        key="results_qa_variable",
    )
    filtered = table[table["Status"].isin(selected)]
    if variable is not None:
        filtered = filtered[filtered["Variable"] == variable]
    if not checks:
        st.info(
            "No QA checks were recorded. Variable statuses and declared evaluation results are still available.",
            icon=":material/info:",
        )
    st.dataframe(filtered, hide_index=True, width="stretch", alt=alt)


def render_json_download(
    label: str,
    value: dict | list,
    filename: str,
    key: str,
    *,
    indent: int | None = None,
    width: Literal["stretch", "content"] | int = "stretch",
) -> None:
    """Offer a size-capped strict JSON download button."""
    try:
        payload = dump_json_strict(
            value, indent=indent, ensure_ascii=False, separators=(",", ":") if indent is None else None
        ).encode("utf-8")
    except ValueError as exc:
        show_warning(f"Cannot prepare {filename} for download", exc)
        return
    if len(payload) > MAX_JSON_BYTES:
        show_warning(f"{filename} download exceeds the 8 MiB limit.")
        return
    st.download_button(
        label, payload, file_name=filename, mime="application/json",
        key=key, icon=":material/download:", width=width, on_click="ignore",
    )
