"""Display helpers and compatibility exports for backend run results."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bcflow.results import (
    artifact_path,
    declared_artifacts,
    finite_values,
    list_manifests,
    load_manifest,
    read_evaluation,
    read_qa,
)
from app.services._io import resolve_local_path

_METRIC_UNITS = {
    "sd_ratio": "dimensionless ratio",
    "wet_day_freq_diff": "fraction",
}


def latest_manifest(history: list[dict], cwd: str | Path) -> str | None:
    """Return the local manifest path from the newest finished run job."""
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
            return str(resolve_local_path(path, cwd))
    return None


def qa_checks_frame(checks: list[dict]) -> pd.DataFrame:
    """Normalize QA check records into a flat display DataFrame."""
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
    return pd.DataFrame(
        rows, columns=["Status", "Variable", "Check", "Message", "Context"]
    )


def chart_data(data: pd.DataFrame) -> pd.DataFrame:
    """Select up to 100 unique locations with Raw and Corrected series."""
    chart = data.loc[:, ["raw", "corrected"]].copy()
    if {"lat", "lon"}.issubset(data.columns):
        chart["Location"] = data["lat"].astype(str) + ", " + data["lon"].astype(str)
    else:
        chart["Location"] = data["station"].astype(str)
    chart = chart.drop_duplicates(["Location"], keep="first").iloc[:100]
    return chart.rename(columns={"raw": "Raw", "corrected": "Corrected"})


def metric_label(metric: str, record: dict, data: pd.DataFrame) -> str:
    """Format an evaluation metric axis label with its units."""
    units = _METRIC_UNITS.get(metric)
    if units is None:
        units = record.get("units")
        if not isinstance(units, str) or not units.strip():
            recorded = data["units"].dropna().unique() if "units" in data else []
            units = (
                str(recorded[0])
                if len(recorded) == 1
                else "variable units, not recorded"
            )
    return f"{metric.replace('_', ' ')} ({units})"
