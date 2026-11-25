"""Headless results-page tests without requiring the parent navigation app."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest

pytest.importorskip("streamlit", minversion="1.67")
from streamlit.testing.v1 import AppTest

import app
from app.ui import results

pytestmark = pytest.mark.usefixtures("web_component_discovery_cache")


def _page():
    from app.ui.results import render

    render()


def _write_run(folder: Path, *, checks=None, variables=None, status="ok") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    variables = (
        variables
        if variables is not None
        else {
            "tmax": {
                "status": "ok",
                "evaluation": "tmax/evaluation.csv",
                "units": "degC",
            }
        }
    )
    manifest = {
        "created": "2026-11-08T12:00:00+00:00",
        "status": status,
        "qa": "qa.json",
        "variables": variables,
    }
    path = folder / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "qa.json").write_text(json.dumps(checks or []), encoding="utf-8")
    if (
        "tmax" in variables
        and variables["tmax"].get("evaluation") == "tmax/evaluation.csv"
    ):
        data = folder / "tmax"
        data.mkdir(exist_ok=True)
        pd.DataFrame(
            [
                {
                    "period": period,
                    "station": station,
                    "metric": metric,
                    "raw": raw,
                    "corrected": corrected,
                }
                for period in ("calibration", "validation")
                for station in ("001", "002")
                for metric, raw, corrected in (
                    ("mean_bias", 2.0, 0.5),
                    ("sd_ratio", 1.5, 1.0),
                )
            ]
        ).to_csv(data / "evaluation.csv", index=False)
    return path


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    fake = ModuleType("app.runtime")
    jobs = []
    manager = SimpleNamespace(cwd=tmp_path, root=tmp_path, history=lambda: list(jobs))
    fake.__dict__.update(
        get_manager=lambda: manager,
        current_config=lambda: ({"output": {"path": str(tmp_path / "outputs")}}, None),
        jobs=jobs,
    )
    monkeypatch.setitem(sys.modules, "app.runtime", fake)
    monkeypatch.setattr(app, "runtime", fake, raising=False)
    results._list_manifests.clear()
    results._read_evaluation.clear()
    yield fake
    results._list_manifests.clear()
    results._read_evaluation.clear()


def _app() -> AppTest:
    # Include cold chart imports on Windows-mounted/CI filesystems.
    app = AppTest.from_function(_page, default_timeout=45).run()
    assert not app.exception
    return app


def _tables(app):
    return [element.value for element in app.dataframe]


def _status_table(app):
    return next(
        table for table in _tables(app) if list(table.columns) == ["Variable", "Status"]
    )


def _preview(app):
    return next(
        table
        for table in _tables(app)
        if {"metric", "raw", "corrected"}.issubset(table.columns)
    )


def test_missing_and_invalid_manual_manifests_are_useful_warnings(runtime, tmp_path):
    app = _app()
    assert any("No saved runs" in item.value for item in app.info)
    app.text_input(key="results_manual_manifest").set_value("missing.json").run()
    assert not app.exception
    assert any(
        "Cannot open this manifest" in item.value and "missing.json" in item.value
        for item in app.warning
    )
    bad = tmp_path / "bad.json"
    bad.write_text("{invalid", encoding="utf-8")
    app.text_input(key="results_manual_manifest").set_value(str(bad)).run()
    assert not app.exception
    assert any("JSON artifact" in item.value for item in app.warning)
    bad.write_text('{"variables": []}', encoding="utf-8")
    app.run()
    assert not app.exception
    assert any("variables" in item.value for item in app.warning)


def test_empty_qa_does_not_hide_status_or_evaluation(runtime, tmp_path):
    _write_run(tmp_path / "outputs/run_empty_qa")
    app = _app()
    assert _status_table(app).to_dict("records") == [
        {"Variable": "tmax", "Status": "ok"}
    ]
    assert any("No QA checks" in item.value for item in app.info)
    assert {item.label: item.value for item in app.metric} == {
        "Total checks": "0",
        "Passed": "0",
        "Warnings": "0",
        "Failed": "0",
    }
    assert len(_preview(app)) == 2
    assert app.get("download_button")
    assert app.get("vega_lite_chart")
    assert all(element.proto.alt for element in app.dataframe)


def test_cancelled_and_skipped_variables_are_not_shown_as_successes(runtime, tmp_path):
    variables = {
        "tmax": {"status": "ok", "evaluation": "tmax/evaluation.csv"},
        "precip": {"status": "failed"},
        "wind": {"status": "cancelled"},
        "solar": {"status": "skipped"},
    }
    _write_run(
        tmp_path / "outputs/run_cancelled", variables=variables, status="cancelled"
    )
    app = _app()
    assert _status_table(app)["Status"].tolist() == [
        "ok",
        "failed",
        "cancelled",
        "skipped",
    ]
    assert any("cancelled" in item.value for item in app.warning)
    assert app.selectbox(key="results_evaluation_variable").options == ["tmax"]
    assert not app.success
    assert not app.exception


def test_qa_counts_and_filters_are_independent_of_evaluation(runtime, tmp_path):
    checks = [
        {
            "name": "a",
            "status": "pass",
            "message": "fine",
            "context": {"variable": "tmax"},
        },
        {
            "name": "b",
            "status": "warn",
            "message": "review",
            "context": {"variable": "tmax"},
        },
        {
            "name": "c",
            "status": "fail",
            "message": "missing",
            "context": {"variable": "precip"},
        },
    ]
    _write_run(tmp_path / "outputs/run_checks", checks=checks)
    app = _app()
    assert {item.label: item.value for item in app.metric} == {
        "Total checks": "3",
        "Passed": "1",
        "Warnings": "1",
        "Failed": "1",
    }
    app.multiselect(key="results_qa_status").set_value(["warn", "fail"]).run()
    app.selectbox(key="results_qa_variable").select("tmax").run()
    assert not app.exception
    qa = next(table for table in _tables(app) if "Check" in table)
    assert qa["Check"].tolist() == ["b"]
    assert len(_preview(app)) == 2


def test_latest_completed_run_outside_folder_is_included_and_history_stays_live(
    runtime, tmp_path
):
    _write_run(tmp_path / "outputs/run_folder")
    external = _write_run(tmp_path / "elsewhere/run_latest")
    app = _app()
    runtime.jobs.insert(
        0,
        {
            "kind": "run",
            "active": False,
            "created": "later",
            "summary": {"manifest": str(external)},
        },
    )
    app.run()
    assert not app.exception
    assert any(
        "Latest completed run" in option and str(external) in option
        for option in app.selectbox(key="results_manifest").options
    )
    app.selectbox(key="results_manifest").set_value(str(external)).run()
    assert not app.exception
    assert app.code[0].value == str(external)
    newer = _write_run(tmp_path / "other/run_newer")
    runtime.jobs.insert(
        0, {"kind": "validate", "summary": {"manifest": "ignored.json"}}
    )
    runtime.jobs.insert(
        0, {"kind": "run", "active": True, "summary": {"manifest": "active.json"}}
    )
    runtime.jobs.insert(
        0, {"kind": "run", "active": False, "summary": {"manifest": str(newer)}}
    )
    app.run()
    assert not app.exception
    assert any(
        str(newer) in option for option in app.selectbox(key="results_manifest").options
    )
    assert not any(
        "active.json" in option or "ignored.json" in option
        for option in app.selectbox(key="results_manifest").options
    )


def test_refresh_bypasses_shallow_listing_cache(runtime, tmp_path):
    app = _app()
    new = _write_run(tmp_path / "outputs/run_new")
    app.button(key="results_refresh").click().run()
    assert not app.exception
    assert any(
        str(new) in option for option in app.selectbox(key="results_manifest").options
    )


def test_current_config_default_and_parse_error_fallback(
    runtime, tmp_path, monkeypatch
):
    target = tmp_path / "different"
    runtime.current_config = lambda: ({"output": {"path": str(target)}}, None)
    app = _app()
    assert app.text_input(key="results_output_folder").value == str(target)
    runtime.current_config = lambda: (None, "bad editor YAML")
    monkeypatch.chdir(tmp_path)
    app = _app()
    assert app.text_input(key="results_output_folder").value == str(
        tmp_path / "outputs"
    )
    assert any("could not be parsed" in item.value for item in app.caption)


def test_chart_filters_one_period_and_metric_and_uses_matching_units(
    runtime, tmp_path, monkeypatch
):
    _write_run(tmp_path / "outputs/run_metrics")
    charts = []
    original = results.st.bar_chart

    def capture(data, **kwargs):
        charts.append((data.copy(), kwargs))
        return original(data, **kwargs)

    monkeypatch.setattr(results.st, "bar_chart", capture)
    app = _app()
    assert set(_preview(app)["metric"]) == {"mean_bias"}
    assert set(_preview(app)["period"]) == {"calibration"}
    assert charts[-1][1]["stack"] is False
    assert charts[-1][1]["width"] == "stretch"
    assert "degC" in charts[-1][1]["y_label"]
    assert charts[-1][1]["alt"]
    app.selectbox(key="results_evaluation_period").select("validation").run()
    app.selectbox(key="results_evaluation_metric").select("sd_ratio").run()
    assert not app.exception
    assert set(_preview(app)["metric"]) == {"sd_ratio"}
    assert set(_preview(app)["period"]) == {"validation"}
    assert charts[-1][0]["Raw"].tolist() == [1.5, 1.5]
    assert "dimensionless" in charts[-1][1]["y_label"]
    assert "degC" not in charts[-1][1]["y_label"]


def test_large_evaluation_is_bounded_and_chart_is_capped(
    runtime, tmp_path, monkeypatch
):
    manifest = _write_run(tmp_path / "outputs/run_large")
    csv = manifest.parent / "tmax/evaluation.csv"
    pd.DataFrame(
        {
            "period": ["validation"] * 50002,
            "station": [f"{index:05d}" for index in range(50002)],
            "metric": ["mean_bias"] * 50002,
            "raw": [float("inf"), -float("inf"), *([2.0] * 50000)],
            "corrected": [0.5] * 50002,
        }
    ).to_csv(csv, index=False)
    charts = []
    monkeypatch.setattr(
        results.st,
        "bar_chart",
        lambda data, **kwargs: charts.append((data.copy(), kwargs)),
    )
    app = _app()
    preview = _preview(app)
    assert len(preview) == 50000
    assert preview["raw"].iloc[:2].isna().all()
    assert len(charts[-1][0]) == 100
    assert charts[-1][0]["Location"].iloc[0] == "00000"
    assert charts[-1][0]["Raw"].iloc[:2].isna().all()
    assert any("50,000" in item.value for item in app.warning)
    assert any("maximum 100" in item.value for item in app.caption)


def test_evaluation_cache_invalidates_for_csv_and_manifest_mtime(
    runtime, tmp_path, monkeypatch
):
    manifest = _write_run(tmp_path / "outputs/run_cache")
    csv = manifest.parent / "tmax/evaluation.csv"
    original = results.service.read_evaluation
    calls = []

    def read(*args, **kwargs):
        calls.append(kwargs["max_rows"])
        return original(*args, **kwargs)

    monkeypatch.setattr(results.service, "read_evaluation", read)
    app = _app()
    app.selectbox(key="results_evaluation_metric").select("sd_ratio").run()
    assert not app.exception
    assert calls == [50000]
    info = csv.stat()
    csv.write_text(
        csv.read_text(encoding="utf-8").replace("1.5", "1.8"), encoding="utf-8"
    )
    os.utime(csv, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
    app.run()
    assert not app.exception
    assert calls == [50000, 50000]
    assert _preview(app)["raw"].iloc[0] == 1.8
    info = manifest.stat()
    os.utime(manifest, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
    app.run()
    assert not app.exception
    assert calls == [50000, 50000, 50000]


def test_unsafe_artifact_and_missing_qa_warn_without_hiding_status(runtime, tmp_path):
    manifest = _write_run(
        tmp_path / "outputs/run_unsafe",
        variables={
            "tmax": {
                "status": "ok",
                "evaluation": "../escape.csv",
                "scen": {"netcdf": "/outside.nc"},
            }
        },
    )
    (manifest.parent / "qa.json").unlink()
    app = _app()
    assert not app.exception
    assert not _status_table(app).empty
    assert any("Cannot read QA" in item.value for item in app.warning)
    assert any("Cannot read this variable" in item.value for item in app.warning)
    assert any("Unavailable artifact" in item.value for item in app.warning)
    assert not app.get("vega_lite_chart")


def test_only_json_downloads_and_no_netcdf_reads(runtime, tmp_path, monkeypatch):
    manifest = _write_run(tmp_path / "outputs/run_downloads")
    nc = manifest.parent / "tmax/scen.nc"
    nc.write_bytes(b"not a dataset")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["variables"]["tmax"]["scen"] = {"netcdf": "tmax/scen.nc"}
    manifest.write_text(json.dumps(document), encoding="utf-8")
    original_open = Path.open
    original_download = results.st.download_button
    downloads = []

    def safe_open(path, *args, **kwargs):
        assert path.suffix != ".nc", (
            "The results page must not open scientific datasets"
        )
        return original_open(path, *args, **kwargs)

    def capture(label, data, **kwargs):
        downloads.append((data, kwargs))
        return original_download(label, data, **kwargs)

    monkeypatch.setattr(Path, "open", safe_open)
    monkeypatch.setattr(results.st, "download_button", capture)
    app = _app()
    assert {kwargs["file_name"] for _, kwargs in downloads} == {
        "manifest.json",
        "qa.json",
    }
    assert all(len(data) <= 8 * 1024 * 1024 for data, _ in downloads)
    assert all(kwargs["mime"] == "application/json" for _, kwargs in downloads)
    artifact_table = next(table for table in _tables(app) if "Local path" in table)
    assert str(nc) in artifact_table["Local path"].tolist()
    assert not app.get("link_button")
    assert not app.exception


def test_oversized_manifest_is_rejected_without_download(runtime, tmp_path):
    manifest = _write_run(tmp_path / "external/run_large")
    with manifest.open("ab") as stream:
        stream.write(b" " * (8 * 1024 * 1024))
    runtime.jobs.append({"kind": "run", "summary": {"manifest": str(manifest)}})
    app = _app()
    assert any("8 MiB" in item.value for item in app.warning)
    assert not app.get("download_button")


def test_grid_locations_are_supported_and_nonfinite_values_are_sanitized():
    data = pd.DataFrame(
        {
            "lat": [10.5, 11.5],
            "lon": [106.5, 107.5],
            "raw": [float("inf"), 2.0],
            "corrected": [1.0, 0.5],
        }
    )
    finite = results._finite_values(data)
    chart = results._chart_data(finite)
    assert chart["Location"].tolist() == ["10.5, 106.5", "11.5, 107.5"]
    assert pd.isna(chart["Raw"].iloc[0])
    assert data["raw"].iloc[0] == float("inf")
    assert results._metric_label("wet_day_freq_diff", {}, data).endswith("(fraction)")
