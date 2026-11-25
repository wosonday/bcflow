"""Live UI polling; workers and pipe readers never call Streamlit."""

from __future__ import annotations

import streamlit as st

from app import runtime
from app.runtime import SessionKeys
from app.services.results import qa_checks_frame
from app.services.runner import latest_cell_progress
from app.ui.components import render_validation_messages, render_variable_status_table


def _show_summary(job: dict) -> None:
    summary = job.get("summary")
    if not isinstance(summary, dict):
        if not job["active"]:
            st.warning(
                "The backend did not provide a usable final response. Check the diagnostic log."
            )
        return
    if job["kind"] == "validate":
        if summary.get("valid"):
            st.success("Validation passed. Return to Configure to run this draft.")
        render_validation_messages(summary)
    elif job["kind"] == "inspect":
        st.json(summary, expanded=True)
    else:
        variables = summary.get("variables", {})
        if variables:
            render_variable_status_table(variables, alt="Final status of each corrected variable")
        if summary.get("run_dir"):
            st.code(summary["run_dir"], language=None)
            st.caption(
                "Outputs are on the server's local filesystem. Open Results to inspect the manifest and evaluation."
            )
        checks = summary.get("qa", [])
        if checks:
            with st.expander("QA checks"):
                st.dataframe(
                    qa_checks_frame(checks), hide_index=True, width="stretch", alt="Run quality-assurance checks"
                )
        if summary.get("error"):
            st.error(summary["error"])


def _render_monitor_body(manager, history: list[dict]) -> None:
    if not history:
        st.info("No jobs yet. Open Configure, load a config, and validate it first.")
        return
    ids = [job["id"] for job in history]
    tracked = st.session_state.get(SessionKeys.TRACKED_JOB_ID)
    if tracked in ids and st.session_state.get(SessionKeys.MONITOR_TRACKING) != tracked:
        st.session_state[SessionKeys.JOB_CHOICE] = tracked
        st.session_state[SessionKeys.MONITOR_TRACKING] = tracked
    if st.session_state.get(SessionKeys.JOB_CHOICE) not in ids:
        st.session_state[SessionKeys.JOB_CHOICE] = ids[0]
    labels = {job["id"]: f"{job['kind']} · {job['status']} · {job['created_at']}" for job in history}
    selected = st.selectbox("Job", ids, format_func=lambda value: labels[value], key=SessionKeys.JOB_CHOICE)
    job = next((item for item in history if item["id"] == selected), None)
    if job is None:
        st.warning("This job is no longer in the in-memory history.")
        return
    st.subheader(f"{job['kind'].capitalize()} · {job['status']}")
    with st.container(horizontal=True):
        st.metric("Elapsed", f"{job['elapsed_seconds']:.1f} s")
        st.metric("Exit code", "—" if job["returncode"] is None else str(job["returncode"]))
    if job["active"]:
        if job["status"] == "cancelling":
            st.warning(
                "Cancellation requested. Waiting for the current operation; completed variables will be saved."
            )
        if st.button(
            "Request cancellation", icon=":material/stop_circle:", key=f"cancel_{selected}", disabled=job["status"] == "cancelling"
        ):
            try:
                manager.request_cancel(selected)
                st.rerun()
            except (OSError, KeyError) as exc:
                st.error(f"Cannot request cancellation: {str(exc)[:500]}")
    elif job["status"] == "ok":
        st.success("Job completed successfully.")
    elif job["status"] == "cancelled":
        st.warning("Job cancelled. Only completed work confirmed by the backend is available.")
    else:
        st.error(job.get("error") or "The job did not complete successfully.")

    progress = job.get("progress") or {}
    if progress:
        variable = progress.get("variable", "")
        st.caption(f"{variable}: {progress.get('stage', '').replace('_', ' ')}")
        if "workers" in progress:
            st.caption(
                f"Workers: {progress['workers']} · CPU limit: {progress.get('cpu_limit', '—')} · "
                f"RAM limit: {progress.get('memory_limit', '—')} · "
                f"estimated task: {progress.get('estimated_task_bytes', 0) / 1024**3:.2f} GiB"
            )
        cells = latest_cell_progress(job, variable)
        if cells and cells["total_cells"]:
            completed, total = cells["completed_cells"], cells["total_cells"]
            st.progress(
                min(1.0, max(0.0, completed / total)),
                text=f"{completed:,}/{total:,} cells · block {cells['block_index']}/{cells['total_blocks']}",
            )
            st.caption(
                "Cell completion, not an estimate of total runtime. Parallel batches report completion after they return."
            )
        elif progress.get("total"):
            completed, total = progress.get("completed", 0), progress["total"]
            st.progress(
                min(1.0, max(0.0, completed / total)),
                text=f"{completed:,}/{total:,} {progress.get('unit', '')}",
            )
    _show_summary(job)
    st.caption(f"Job artifacts: {job['job_dir']}")
    with st.expander("Diagnostic log", expanded=bool(job.get("error"))):
        st.code(job.get("logs") or "No diagnostic output.", language=None, wrap_lines=True)
    with st.expander("Recent structured events"):
        st.json(job.get("events", [])[-20:], expanded=False)


@st.fragment(run_every=1)
def _live_monitor_polling() -> None:
    manager = runtime.get_manager()
    history = manager.history()
    _render_monitor_body(manager, history)
    if not any(job["active"] for job in history):
        st.rerun()


def render() -> None:
    st.title("Run & monitor")
    st.caption(
        "One shared local backend job at a time. Ordinary UI reruns and browser refreshes do not restart it; server restarts do not resume it."
    )
    runtime.init_session()
    manager = runtime.get_manager()
    history = manager.history()
    if any(job["active"] for job in history):
        _live_monitor_polling()
    else:
        _render_monitor_body(manager, history)
