"""Native, read-only results page, called by the navigation page's script."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pandas as pd
import streamlit as st

from app.services import results as service

_MANUAL = "Enter a manifest path"
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_ROWS = 50000


@st.cache_data(ttl=5, max_entries=20, show_spinner=False)
def _list_manifests(root: str) -> list[dict]:
    return service.list_manifests(root)


@st.cache_data(max_entries=10, show_spinner=False)
def _read_evaluation(
    manifest_path: str,
    variable: str,
    manifest_mtime: int,
    csv_path: str,
    csv_mtime: int,
) -> pd.DataFrame:
    # All arguments participate in the cache key, including both file mtimes.
    return service.read_evaluation(manifest_path, variable, max_rows=_MAX_ROWS)


def _local_path(value: str, cwd: Path) -> Path:
    path = Path(value).expanduser()
    return (cwd / path).absolute()


def _latest_manifest(history: list[dict], cwd: Path) -> str | None:
    for job in history:
        summary = job.get("summary")
        if (
            job.get("kind") != "run"
            or job.get("active")
            or not isinstance(summary, dict)
        ):
            continue
        path = summary.get("manifest")
        if isinstance(path, str) and path:
            return str(_local_path(path, cwd))
    return None


def _warn(message: str, exc: Exception) -> None:
    st.warning(f"{message}: {str(exc)[:500]}", icon=":material/warning:")


def _download_json(label: str, value: dict | list, filename: str, key: str) -> None:
    try:
        payload = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        _warn(f"Cannot prepare {filename} for download", exc)
        return
    if len(payload) > _MAX_JSON_BYTES:
        st.warning(
            f"{filename} download exceeds the 8 MiB limit.", icon=":material/warning:"
        )
        return
    st.download_button(
        label,
        payload,
        file_name=filename,
        mime="application/json",
        key=key,
        icon=":material/download:",
        width="stretch",
        on_click="ignore",
    )


def _render_status(manifest: dict) -> None:
    st.subheader("Run status", anchor=False)
    status = manifest.get("status", "unknown")
    st.caption(
        f"Run status: {status}. Created: {manifest.get('created') or 'not recorded'}."
    )
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
    table = pd.DataFrame(
        [
            {"Variable": name, "Status": record.get("status", "unknown")}
            for name, record in manifest["variables"].items()
        ],
        columns=["Variable", "Status"],
    )
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        alt="Status of every variable, including cancelled and skipped variables",
    )


def _render_qa(manifest_path: str, manifest: dict) -> None:
    st.subheader("Quality checks", anchor=False)
    try:
        checks = service.read_qa(manifest_path)
    except (ValueError, OSError) as exc:
        _warn("Cannot read QA checks", exc)
        return
    rows = []
    for check in checks:
        context = check.get("context")
        context = context if isinstance(context, dict) else {}
        rows.append(
            {
                "Status": str(check.get("status", "unknown")),
                "Variable": str(context.get("variable") or "Run"),
                "Check": str(check.get("name", "")),
                "Message": str(check.get("message", "")),
                "Context": json.dumps(context, ensure_ascii=False),
            }
        )
    table = pd.DataFrame(
        rows, columns=["Status", "Variable", "Check", "Message", "Context"]
    )
    with st.container(horizontal=True):
        st.metric("Total checks", len(checks))
        for status, label in (
            ("pass", "Passed"),
            ("warn", "Warnings"),
            ("fail", "Failed"),
        ):
            st.metric(label, sum(row["Status"] == status for row in rows))
    statuses = list(dict.fromkeys(["pass", "warn", "fail", *table["Status"].tolist()]))
    selected = st.multiselect(
        "QA status", statuses, default=statuses, key="results_qa_status"
    )
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
    st.dataframe(
        filtered,
        hide_index=True,
        width="stretch",
        alt="Quality checks filtered by status and variable",
    )
    if manifest.get("qa") is not None:
        _download_json("Download QA JSON", checks, "qa.json", "results_download_qa")


def _finite_values(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    columns = data.select_dtypes(include="number").columns
    data[columns] = data[columns].replace([float("inf"), -float("inf")], float("nan"))
    return data


def _chart_data(data: pd.DataFrame) -> pd.DataFrame:
    chart = data.loc[:, ["raw", "corrected"]].copy()
    if {"lat", "lon"}.issubset(data.columns):
        chart["Location"] = data["lat"].astype(str) + ", " + data["lon"].astype(str)
    else:
        chart["Location"] = data["station"].astype(str)
    chart = chart.drop_duplicates(["Location"], keep="first").iloc[:100]
    return chart.rename(columns={"raw": "Raw", "corrected": "Corrected"})


def _metric_label(metric: str, record: dict, data: pd.DataFrame) -> str:
    if metric == "sd_ratio":
        units = "dimensionless ratio"
    elif metric == "wet_day_freq_diff":
        units = "fraction"
    else:
        units = record.get("units")
        if not isinstance(units, str) or not units.strip():
            recorded = data["units"].dropna().unique() if "units" in data else []
            units = (
                str(recorded[0])
                if len(recorded) == 1
                else "variable units, not recorded"
            )
    return f"{metric.replace('_', ' ')} ({units})"


def _render_evaluation(manifest_path: str, manifest: dict) -> None:
    st.subheader("Evaluation", anchor=False)
    variables = [
        name
        for name, record in manifest["variables"].items()
        if record.get("evaluation") is not None
    ]
    if not variables:
        st.info("No evaluation CSV is declared for this run.", icon=":material/info:")
        return
    variable = st.selectbox(
        "Evaluation variable", variables, key="results_evaluation_variable"
    )
    if variable is None:
        return
    record = manifest["variables"][variable]
    try:
        csv = service.artifact_path(manifest_path, record["evaluation"])
        data = _read_evaluation(
            manifest_path,
            variable,
            Path(manifest_path).stat().st_mtime_ns,
            str(csv),
            csv.stat().st_mtime_ns,
        )
    except (ValueError, OSError, RuntimeError) as exc:
        _warn("Cannot read this variable's evaluation", exc)
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
            "Evaluation period",
            periods,
            format_func=lambda item: item or "Unspecified period",
            key="results_evaluation_period",
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
        filtered,
        hide_index=True,
        width="stretch",
        alt=f"{metric} evaluation preview for {variable}, raw and corrected values",
    )
    chart = _chart_data(filtered)
    if chart.empty:
        return
    st.bar_chart(
        chart,
        x="Location",
        y=["Raw", "Corrected"],
        x_label="Location",
        y_label=_metric_label(metric, record, filtered),
        stack=False,
        sort=False,
        width="stretch",
        alt=f"Raw versus corrected {metric} for {variable} in the selected period",
    )
    st.caption(
        f"Showing the first {len(chart):,} locations (maximum 100); one period and one metric only. If a location repeats, its first row is shown. Non-finite values are displayed as missing."
    )
    if "not recorded" in _metric_label(metric, record, filtered):
        st.caption(
            "Units were not recorded in this evaluation. Values retain the variable's source units; no unit conversion is applied."
        )


def _render_artifacts(manifest_path: str, manifest: dict) -> None:
    with st.expander("Artifact paths"):
        st.caption(
            "Paths are shown for local use only. Scientific datasets are never opened or offered as downloads."
        )
        declared = []
        for key in ("qa", "config"):
            if manifest.get(key) is not None:
                declared.append((key, manifest[key]))
        for name, record in manifest["variables"].items():
            for key in ("evaluation", "adj", "scen", "hist_eval"):
                value = record.get(key)
                if isinstance(value, dict):
                    declared.extend(
                        (f"{name} / {key} / {fmt}", path)
                        for fmt, path in value.items()
                        if path is not None
                    )
                elif value is not None:
                    declared.append((f"{name} / {key}", value))
        rows = []
        for label, relative in declared:
            try:
                path = service.artifact_path(manifest_path, relative)
            except (ValueError, OSError, RuntimeError) as exc:
                _warn(f"Unavailable artifact ({label})", exc)
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
    st.caption(
        "Inspect saved run status, quality checks and bounded evaluation previews."
    )
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
    try:
        root = _local_path(folder, cwd)
        manifests = _list_manifests(str(root))
        latest = _latest_manifest(manager.history(), cwd)
    except (ValueError, OSError, RuntimeError) as exc:
        _warn("Cannot discover saved runs", exc)
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

    selected = st.selectbox(
        "Saved run", [*paths, _MANUAL], format_func=label, key="results_manifest"
    )
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
        manifest_path = str(_local_path(selected, cwd))
        manifest = service.load_manifest(manifest_path)
    except (ValueError, OSError, RuntimeError) as exc:
        _warn("Cannot open this manifest", exc)
        return
    st.code(manifest_path, language=None)
    _render_status(manifest)
    _download_json(
        "Download manifest JSON", manifest, "manifest.json", "results_download_manifest"
    )
    _render_qa(manifest_path, manifest)
    _render_evaluation(manifest_path, manifest)
    _render_artifacts(manifest_path, manifest)
