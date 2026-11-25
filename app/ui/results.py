"""Native, read-only results page, called by the navigation page's script."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pandas as pd
import streamlit as st

from app.services import results as service
from app.services._io import resolve_local_path
from app.ui.components import (
    render_json_download,
    render_qa_panel,
    render_variable_status_table,
    show_warning,
)

_MANUAL = "Enter a manifest path"
_MAX_ROWS = 50000

# Aliases kept at module level for existing unit/UI tests.
_finite_values = service.finite_values
_chart_data = service.chart_data
_metric_label = service.metric_label


def _file_stamp(path: str | Path) -> tuple[int, int]:
    info = Path(path).stat()
    return info.st_mtime_ns, info.st_size


@st.cache_data(ttl=5, max_entries=20, show_spinner=False)
def _list_manifests(root: str) -> list[dict]:
    return service.list_manifests(root)


@st.cache_data(max_entries=10, show_spinner=False)
def _read_qa(
    manifest_path: str, manifest_stamp: tuple[int, int], qa_path: str, qa_stamp: tuple[int, int]
) -> list[dict]:
    # Include both file mtimes and sizes in the cache key.
    return service.read_qa(manifest_path)


@st.cache_data(max_entries=10, show_spinner=False)
def _read_evaluation(
    manifest_path: str,
    variable: str,
    manifest_stamp: tuple[int, int],
    csv_path: str,
    csv_stamp: tuple[int, int],
) -> pd.DataFrame:
    # Include both file mtimes and sizes in the cache key.
    return service.read_evaluation(manifest_path, variable, max_rows=_MAX_ROWS)


def _render_status(manifest: dict) -> None:
    st.subheader("Run status", anchor=False)
    status = manifest.get("status", "unknown")
    st.caption(f"Run status: {status}. Created: {manifest.get('created') or 'not recorded'}.")
    if status == "cancelled":
        st.warning(
            "This run was cancelled. Completed results remain available; cancelled and skipped variables are not successful outputs.",
            icon=":material/cancel:",
        )
    elif status in {"failed", "error"}:
        st.warning(
            "This run did not complete successfully. Check each variable's status and QA checks.",
            icon=":material/warning:",
        )
    statuses = {name: record.get("status", "unknown") for name, record in manifest["variables"].items()}
    render_variable_status_table(statuses, alt="Status of every variable, including cancelled and skipped variables")


@st.fragment
def _render_qa(manifest_path: str, manifest: dict) -> None:
    st.subheader("Quality checks", anchor=False)
    try:
        relative_qa = manifest.get("qa")
        if relative_qa is None:
            checks = []
        else:
            qa_file = service.artifact_path(manifest_path, relative_qa)
            checks = _read_qa(
                manifest_path, _file_stamp(manifest_path), str(qa_file), _file_stamp(qa_file)
            )
    except (ValueError, OSError, RuntimeError) as exc:
        show_warning("Cannot read QA checks", exc)
        return
    render_qa_panel(checks, alt="Quality checks filtered by status and variable")
    if manifest.get("qa") is not None:
        render_json_download("Download QA JSON", checks, "qa.json", "results_download_qa")


@st.fragment
def _render_evaluation(manifest_path: str, manifest: dict) -> None:
    st.subheader("Evaluation", anchor=False)
    variables = [name for name, record in manifest["variables"].items() if record.get("evaluation") is not None]
    if not variables:
        st.info("No evaluation CSV is declared for this run.", icon=":material/info:")
        return
    variable = st.selectbox("Evaluation variable", variables, key="results_evaluation_variable")
    if variable is None:
        return
    record = manifest["variables"][variable]
    try:
        csv = service.artifact_path(manifest_path, record["evaluation"])
        data = _read_evaluation(manifest_path, variable, _file_stamp(manifest_path), str(csv), _file_stamp(csv))
    except (ValueError, OSError, RuntimeError) as exc:
        show_warning("Cannot read this variable's evaluation", exc)
        return
    if data.attrs.get("truncated"):
        st.warning(
            "Evaluation preview is limited to the first 50,000 rows. Filters apply only to these rows.",
            icon=":material/warning:",
        )
    data = _finite_values(data)
    if data.empty:
        st.info("The evaluation CSV contains no rows.", icon=":material/info:")
        return
    if "period" in data:
        periods = data["period"].fillna("").astype(str).drop_duplicates().tolist()
        period = st.selectbox(
            "Evaluation period", periods, format_func=lambda item: item or "Unspecified period", key="results_evaluation_period"
        )
        data = data.loc[data["period"].fillna("").astype(str) == period, :]
    metrics = data["metric"].dropna().astype(str).drop_duplicates().tolist()
    if not metrics:
        st.info("No metrics are available for this period.", icon=":material/info:")
        return
    metric = st.selectbox("Evaluation metric", metrics, key="results_evaluation_metric")
    if metric is None:
        return
    filtered = data.loc[data["metric"].astype(str) == metric, :]
    st.dataframe(
        filtered, hide_index=True, width="stretch",
        alt=f"{metric} evaluation preview for {variable}, raw and corrected values",
    )
    chart = _chart_data(filtered)
    if chart.empty:
        return
    y_label = _metric_label(metric, record, filtered)
    st.bar_chart(
        chart, x="Location", y=["Raw", "Corrected"], x_label="Location", y_label=y_label,
        stack=False, sort=False, width="stretch",
        alt=f"Raw versus corrected {metric} for {variable} in the selected period",
    )
    st.caption(
        f"Showing the first {len(chart):,} locations (maximum 100); one period and one metric only. If a location repeats, its first row is shown. Non-finite values are displayed as missing."
    )
    if "not recorded" in y_label:
        st.caption(
            "Units were not recorded in this evaluation. Values retain the variable's source units; no unit conversion is applied."
        )


def _render_artifacts(manifest_path: str, manifest: dict) -> None:
    with st.expander("Artifact paths"):
        st.caption(
            "Paths are shown for local use only. Scientific datasets are never opened or offered as downloads."
        )
        rows = []
        for label, relative in service.declared_artifacts(manifest):
            try:
                path = service.artifact_path(manifest_path, relative)
            except (ValueError, OSError, RuntimeError) as exc:
                show_warning(f"Unavailable artifact ({label})", exc)
                continue
            rows.append({"Artifact": label, "Local path": str(path)})
        st.dataframe(
            pd.DataFrame(rows, columns=["Artifact", "Local path"]),
            hide_index=True,
            width="stretch",
            alt="Validated local artifact paths; no file links or scientific dataset downloads",
        )


def render() -> None:
    """Render results using the parent app's runtime and current editor state."""
    runtime = import_module("app.runtime")

    st.title(":material/analytics: Results")
    st.caption("Inspect saved run status, quality checks and bounded evaluation previews.")
    manager = runtime.get_manager()
    config, config_error = runtime.current_config()
    cwd = Path(manager.cwd)
    output = config.get("output") if config else None
    default = output.get("path") if isinstance(output, dict) else None
    if not isinstance(default, str) or not default:
        default = str(Path.cwd() / "outputs")
    if config_error:
        st.caption(
            "The current editor config could not be parsed. You can still browse saved results."
        )
    folder = st.text_input("Output folder", value=default, key="results_output_folder")
    if st.button("Refresh runs", icon=":material/refresh:", key="results_refresh"):
        _list_manifests.clear()
        _read_qa.clear()
        _read_evaluation.clear()
    try:
        root = resolve_local_path(folder, cwd)
        manifests = _list_manifests(str(root))
        latest = service.latest_manifest(manager.history(), cwd)
    except (ValueError, OSError, RuntimeError) as exc:
        show_warning("Cannot discover saved runs", exc)
        manifests, latest = [], None
    paths = list(
        dict.fromkeys(([latest] if latest else []) + [row["path"] for row in manifests])
    )
    metadata = {row["path"]: row for row in manifests}

    def label(path: str) -> str:
        if path == _MANUAL:
            return path
        row = metadata.get(path, {})
        prefix = "Latest completed run · " if path == latest else ""
        return f"{prefix}{Path(path).parent.name} · {row.get('status', 'saved run')} · {path}"

    selected = st.selectbox("Saved run", [*paths, _MANUAL], format_func=label, key="results_manifest")
    if selected == _MANUAL:
        if not paths:
            st.info(
                "No saved runs were found here. Only manifest.json and run_*/manifest.json are checked; enter a manifest path below to open another run.",
                icon=":material/folder_open:",
            )
        selected = st.text_input(
            "Manifest path",
            key="results_manual_manifest",
            placeholder="Path to a run's manifest.json",
        )
        if not selected.strip():
            return
    try:
        manifest_path = str(resolve_local_path(selected, cwd))
        manifest = service.load_manifest(manifest_path)
    except (ValueError, OSError, RuntimeError) as exc:
        show_warning("Cannot open this manifest", exc)
        return
    st.code(manifest_path, language=None)
    _render_status(manifest)
    render_json_download("Download manifest JSON", manifest, "manifest.json", "results_download_manifest")
    _render_qa(manifest_path, manifest)
    _render_evaluation(manifest_path, manifest)
    _render_artifacts(manifest_path, manifest)
