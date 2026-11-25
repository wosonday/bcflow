"""Live UI polling; workers and pipe readers never call Streamlit."""

from __future__ import annotations

import streamlit as st

from app import runtime


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
        for error in summary.get("errors", []):
            location = ".".join(map(str, error.get("loc", [])))
            st.error(f"{location}: {error.get('message', '')}")
        for warning in summary.get("warnings", []):
            st.warning(warning.get("message", str(warning)))
    elif job["kind"] == "inspect":
        st.json(summary, expanded=True)
    else:
        variables = summary.get("variables", {})
        if variables:
            st.dataframe(
                [
                    {"Variable": name, "Status": status}
                    for name, status in variables.items()
                ],
                hide_index=True,
                alt="Final status of each corrected variable",
            )
        if summary.get("run_dir"):
            st.code(summary["run_dir"], language=None)
            st.caption(
                "Outputs are on the server's local filesystem. Open Results to inspect the manifest and evaluation."
            )
        checks = summary.get("qa", [])
        if checks:
            with st.expander("QA checks"):
                st.dataframe(
                    checks, hide_index=True, alt="Run quality-assurance checks"
                )
        if summary.get("error"):
            st.error(summary["error"])


@st.fragment(run_every=1)
def _live_monitor() -> None:
    manager = runtime.get_manager()
    history = manager.history()
    if not history:
        st.info("No jobs yet. Open Configure, load a config, and validate it first.")
        return
    ids = [job["id"] for job in history]
    tracked = st.session_state.get("tracked_job_id")
    if tracked in ids and st.session_state.get("_monitor_tracking") != tracked:
        st.session_state["job_choice"] = tracked
        st.session_state["_monitor_tracking"] = tracked
    if st.session_state.get("job_choice") not in ids:
        st.session_state["job_choice"] = ids[0]
    labels = {
        job["id"]: f"{job['kind']} · {job['status']} · {job['created_at']}"
        for job in history
    }
    selected = st.selectbox(
        "Job", ids, format_func=lambda value: labels[value], key="job_choice"
    )
    job = manager.snapshot(selected)
    if job is None:
        st.warning("This job is no longer in the in-memory history.")
        return
    st.subheader(f"{job['kind'].capitalize()} · {job['status']}")
    with st.container(horizontal=True):
        st.metric("Elapsed", f"{job['elapsed_seconds']:.1f} s")
        st.metric(
            "Exit code", "—" if job["returncode"] is None else str(job["returncode"])
        )
    if job["active"]:
        if job["status"] == "cancelling":
            st.warning(
                "Cancellation requested. Waiting for the current operation; completed variables will be saved."
            )
        if st.button(
            "Request cancellation",
            icon=":material/stop_circle:",
            key=f"cancel_{selected}",
            disabled=job["status"] == "cancelling",
        ):
            try:
                manager.request_cancel(selected)
                st.rerun()
            except (OSError, KeyError) as exc:
                st.error(f"Cannot request cancellation: {exc}")
    elif job["status"] == "ok":
        st.success("Job completed successfully.")
    elif job["status"] == "cancelled":
        st.warning(
            "Job cancelled. Only completed work confirmed by the backend is available."
        )
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
        cells = next(
            (
                event
                for event in reversed(job.get("events", []))
                if event.get("event") == "progress"
                and "total_cells" in event
                and event.get("variable") == variable
            ),
            None,
        )
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
        st.code(
            job.get("logs") or "No diagnostic output.", language=None, wrap_lines=True
        )
    with st.expander("Recent structured events"):
        st.json(job.get("events", [])[-20:], expanded=False)


def render() -> None:
    st.title("Run & monitor")
    st.caption(
        "One shared local backend job at a time. Ordinary UI reruns and browser refreshes do not restart it; server restarts do not resume it."
    )
    runtime.init_session()
    _live_monitor()
