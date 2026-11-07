"""Progress, cooperative cancellation, persistence, and CLI signal regressions."""

from __future__ import annotations

import copy
import json
import signal
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr
import yaml

from bcflow import cli, pipeline, service
from bcflow.config_schema import OutputConfig
from bcflow.loaders import scan_grid
from bcflow.progress import RunCancelled, RunControl
from bcflow.writers import write_run


def _write_config(tmp_path, config):
    config = {**config, "output": {"path": str(tmp_path / "outputs")}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _small_result():
    """Exercise the real writer without training a model for orchestration tests."""
    scen = xr.DataArray(
        [[1.0], [2.0]],
        dims=("time", "station"),
        coords={"time": pd.date_range("2020-01-01", periods=2), "station": ["ST01"]},
        attrs={"units": "degC"},
    )
    return {
        "scen": scen,
        "adj": SimpleNamespace(ds=xr.Dataset({"factor": ("station", [1.0])})),
    }


def _read_manifest(summary):
    path = Path(summary["manifest"])
    return path.parent, json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def sigint_handler(monkeypatch):
    """Capture handler installation without changing pytest's real SIGINT handler."""
    previous = object()
    state = {"handler": previous, "previous": previous, "calls": []}

    def install(signum, handler):
        assert signum == signal.SIGINT
        old = state["handler"]
        state["handler"] = handler
        state["calls"].append(handler)
        return old

    monkeypatch.setattr(cli.signal, "signal", install)
    return state


def test_control_progress_has_elapsed_time_and_details():
    events = []
    control = RunControl(events.append)
    assert control.cancelled is False
    assert control.check_cancelled() is None

    control.progress("tmax", "training", block_index=2, total_blocks=3)
    control.progress("tmax", "evaluation", source="hist")

    assert len(events) == 2
    assert (
        events[0].items()
        >= {
            "event": "progress",
            "variable": "tmax",
            "stage": "training",
            "block_index": 2,
            "total_blocks": 3,
        }.items()
    )
    assert events[1]["source"] == "hist"
    elapsed = [event["elapsed_seconds"] for event in events]
    assert all(isinstance(value, (int, float)) and value >= 0 for value in elapsed)
    assert elapsed == sorted(elapsed)


def test_control_cancel_without_callback():
    control = RunControl()
    control.progress("tmax", "training")
    control.request_cancel()
    control.request_cancel()
    assert control.cancelled is True
    assert issubclass(RunCancelled, KeyboardInterrupt)
    assert not issubclass(RunCancelled, Exception)
    with pytest.raises(RunCancelled):
        control.check_cancelled()


def test_progress_checks_cancellation_before_callback():
    events = []
    control = RunControl(events.append)
    control.request_cancel()
    with pytest.raises(RunCancelled):
        control.progress("tmax", "training")
    assert events == []


def test_progress_checks_cancellation_after_callback():
    events = []

    def cancel(event):
        events.append(event)
        control.request_cancel()

    control = RunControl(cancel)
    with pytest.raises(RunCancelled):
        control.progress("tmax", "training")
    assert len(events) == 1
    assert events[0]["stage"] == "training"
    assert control.cancelled is True


def test_scan_grid_reports_initial_and_each_chunk():
    data = xr.DataArray(
        np.array(
            [
                [1.0, np.nan],
                [np.nan, np.nan],
                [3.0, np.nan],
                [4.0, np.nan],
                [5.0, np.nan],
            ]
        ).reshape(5, 1, 2),
        dims=("time", "lat", "lon"),
        coords={"time": pd.date_range("2000-01-01", periods=5)},
    )
    calls = []
    has_data, valid, days = scan_grid(
        data,
        ("2000-01-02", "2000-01-04"),
        chunk=2,
        on_progress=lambda completed, total: calls.append((completed, total)),
    )
    assert calls == [(0, 5), (2, 5), (4, 5), (5, 5)]
    np.testing.assert_array_equal(has_data, [[True, False]])
    np.testing.assert_array_equal(valid, [[2, 0]])
    assert days == 3


def test_scan_grid_propagates_callback_cancellation():
    data = xr.DataArray(
        np.ones((5, 1, 1)),
        dims=("time", "lat", "lon"),
        coords={"time": pd.date_range("2000-01-01", periods=5)},
    )
    calls = []

    def cancel(completed, total):
        calls.append((completed, total))
        if completed == 2:
            raise RunCancelled()

    with pytest.raises(RunCancelled):
        scan_grid(data, None, chunk=2, on_progress=cancel)
    assert calls == [(0, 5), (2, 5)]


def test_real_grid_reports_all_blocks_and_succeeds(grid_config):
    config = copy.deepcopy(grid_config)
    config["variables"] = ["tmax"]
    config["correction"] = {"tmax": config["correction"]["tmax"]}
    events = []
    out = pipeline.run_pipeline(config, on_event=events.append)

    assert out["cancelled"] is False
    assert out["variable_statuses"] == {"tmax": "ok"}
    assert out["results"]["tmax"] is not None
    assert not [check for check in out["qa"] if check.status == "fail"]
    progress = [event for event in events if event["event"] == "progress"]
    assert {
        "loading_reference",
        "loading_model",
        "training",
        "adjustment",
        "evaluation",
        "combining",
    } <= {event["stage"] for event in progress}
    starts = [event for event in progress if event["stage"] == "block_started"]
    finishes = [event for event in progress if event["stage"] == "block_finished"]
    assert len(starts) == len(finishes) == 3
    total = out["results"]["tmax"]["grid"].size
    assert total == 258
    assert [event["block_index"] for event in starts] == [1, 2, 3]
    assert [event["block_index"] for event in finishes] == [1, 2, 3]
    assert [event["block_cells"] for event in starts] == [100, 100, 58]
    assert [event["completed_cells"] for event in starts] == [0, 100, 200]
    assert [event["completed_cells"] for event in finishes] == [100, 200, 258]
    for start, finish in zip(starts, finishes, strict=True):
        assert start["total_blocks"] == finish["total_blocks"] == 3
        assert start["total_cells"] == finish["total_cells"] == total
        assert start["variable"] == finish["variable"] == "tmax"
        assert start["block_cells"] == finish["block_cells"]
        assert events.index(start) < events.index(finish)
    elapsed = [event["elapsed_seconds"] for event in events]
    assert all(value >= 0 for value in elapsed)
    assert elapsed == sorted(elapsed)


def test_real_grid_cancel_between_blocks_discards_partial_variable(
    grid_config,
    tmp_path,
):
    events = []

    def cancel_after_block(event):
        events.append(event)
        if event["event"] == "progress" and event["stage"] == "block_finished":
            control.request_cancel()

    control = RunControl(cancel_after_block)
    out = pipeline.run_pipeline(grid_config, control=control)

    assert out["cancelled"] is True
    assert out["results"] == {}
    assert out["variable_statuses"] == {"tmax": "cancelled", "precip": "skipped"}
    blocks = [
        event
        for event in events
        if event["event"] == "progress"
        and event["stage"] in {"block_started", "block_finished"}
    ]
    assert [event["stage"] for event in blocks] == ["block_started", "block_finished"]
    assert blocks[-1]["completed_cells"] == blocks[-1]["block_cells"] == 100
    assert blocks[-1]["total_cells"] == 258
    assert [
        event["variable"] for event in events if event["event"] == "variable_started"
    ] == ["tmax"]
    assert not any(
        event["event"] == "progress" and event["stage"] == "combining"
        for event in events
    )
    assert events[-1]["event"] == "run_cancelled"
    assert not any(check.name == "pipeline:tmax:exception" for check in out["qa"])

    manifest_path = write_run(
        out, OutputConfig(path=str(tmp_path / "out")), control=control
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
    assert manifest["variables"] == {
        "tmax": {"status": "cancelled"},
        "precip": {"status": "skipped"},
    }
    assert not (manifest_path.parent / "tmax").exists()
    assert not (manifest_path.parent / "precip").exists()


def test_pipeline_precancel_skips_all_variables(toy_config, monkeypatch):
    def must_not_run(*args, **kwargs):
        pytest.fail("Pre-cancelled control must not start a variable")

    monkeypatch.setattr(pipeline, "run_variable", must_not_run)
    events = []
    control = RunControl(events.append)
    control.request_cancel()
    out = pipeline.run_pipeline(toy_config, control=control)
    assert out["cancelled"] is True
    assert out["results"] == {}
    assert out["qa"] == []
    assert out["variable_statuses"] == {"tmax": "skipped", "precip": "skipped"}
    assert not any(event["event"] == "variable_started" for event in events)
    assert events[-1]["event"] == "run_cancelled"


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, RunCancelled])
def test_service_preserves_completed_variable_after_interrupt(
    toy_config,
    tmp_path,
    monkeypatch,
    interruption,
):
    calls = []
    completed = _small_result()

    def run_variable(config, variable, qa, *, control):
        calls.append(variable)
        control.progress(variable, "training")
        if variable == "precip":
            raise interruption()
        return completed

    monkeypatch.setattr(pipeline, "run_variable", run_variable)
    events = []
    summary = service.run_config_file(
        _write_config(tmp_path, toy_config), events.append
    )
    assert calls == ["tmax", "precip"]
    assert summary["status"] == "cancelled"
    assert summary["exit_code"] == 130
    assert summary["error"] is None
    assert summary["variables"] == {"tmax": "ok", "precip": "cancelled"}
    assert summary["counts"]["fail"] == 0
    run_dir, manifest = _read_manifest(summary)
    assert manifest["status"] == "cancelled"
    assert manifest["variables"]["precip"] == {"status": "cancelled"}
    assert not (run_dir / "precip").exists()
    with xr.open_dataset(
        run_dir / manifest["variables"]["tmax"]["scen"]["netcdf"]
    ) as ds:
        np.testing.assert_array_equal(ds["tmax"].values, completed["scen"].values)
    assert (run_dir / "tmax" / "adj.nc").is_file()
    assert (run_dir / "qa.json").is_file()
    assert (run_dir / "config.json").is_file()
    assert any(
        event["event"] == "progress"
        and event["stage"] == "writing"
        and event["variable"] == "tmax"
        for event in events
    )


def test_writer_preserves_all_four_variable_statuses(tmp_path):
    out = {
        "results": {"tmax": _small_result(), "tmin": None},
        "qa": [],
        "cancelled": True,
        "variable_statuses": {
            "tmax": "ok",
            "tmin": "failed",
            "precip": "cancelled",
            "wind": "skipped",
        },
    }
    control = RunControl()
    control.request_cancel()
    path = write_run(out, OutputConfig(path=str(tmp_path)), control=control)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
    assert {
        name: entry["status"] for name, entry in manifest["variables"].items()
    } == out["variable_statuses"]
    assert (path.parent / "tmax" / "scen.nc").is_file()
    for name in ("tmin", "precip", "wind"):
        assert not (path.parent / name).exists()


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_cli_progress_summary_and_completed_outputs(
    toy_config,
    tmp_path,
    monkeypatch,
    capsys,
    sigint_handler,
    json_mode,
    cancel,
):
    calls = []

    def run_variable(config, variable, qa, *, control):
        calls.append(variable)
        if cancel and variable == "precip":
            sigint_handler["handler"](signal.SIGINT, None)
            assert control.cancelled is True
        control.progress(variable, "training")
        return _small_result()

    monkeypatch.setattr(pipeline, "run_variable", run_variable)
    config_path = _write_config(tmp_path, toy_config)
    args = ["run", str(config_path)] + (["--json"] if json_mode else [])
    assert cli.main(args) == (130 if cancel else 0)
    assert calls == ["tmax", "precip"]
    captured = capsys.readouterr()
    expected = {"tmax": "ok", "precip": "cancelled" if cancel else "ok"}
    if json_mode:
        # Parsing every line also detects accidental human progress on stdout.
        events = [json.loads(line) for line in captured.out.splitlines()]
        assert events[0]["event"] == "run_started"
        assert any(
            event["event"] == "progress" and event["stage"] == "training"
            for event in events
        )
        summary = events[-1]
        assert summary["event"] == "run_finished"
        assert summary["status"] == ("cancelled" if cancel else "ok")
        assert summary["exit_code"] == (130 if cancel else 0)
        assert summary["variables"] == expected
        assert "training" not in captured.err
        run_dir, manifest = _read_manifest(summary)
    else:
        assert "tmax: training" in captured.err
        assert "Starting tmax" in captured.err
        assert "tmax: training" not in captured.out
        assert (
            "Run cancelled" in captured.out
            if cancel
            else "Outputs written to:" in captured.out
        )
        paths = list((tmp_path / "outputs").glob("*/manifest.json"))
        assert len(paths) == 1
        run_dir = paths[0].parent
        manifest = json.loads(paths[0].read_text(encoding="utf-8"))
    assert manifest["status"] == ("cancelled" if cancel else "ok")
    assert {
        name: entry["status"] for name, entry in manifest["variables"].items()
    } == expected
    assert (run_dir / "tmax" / "scen.nc").is_file()
    assert (run_dir / "precip").exists() is (not cancel)
    if cancel:
        assert "Cancellation requested" in captured.err
        assert "Forced cancellation" not in captured.err
    assert sigint_handler["handler"] is sigint_handler["previous"]
    assert len(sigint_handler["calls"]) == 2


def test_cli_second_sigint_forces_exit_and_restores_handler(
    monkeypatch,
    tmp_path,
    capsys,
    sigint_handler,
):
    def run_config_file(path, on_event=None, *, control):
        handler = sigint_handler["handler"]
        assert callable(handler)
        assert control.cancelled is False
        handler(signal.SIGINT, None)
        assert control.cancelled is True
        assert issubclass(cli._ForcedCancellation, BaseException)
        assert not issubclass(cli._ForcedCancellation, (Exception, KeyboardInterrupt))
        try:
            handler(signal.SIGINT, None)
        except cli._ForcedCancellation:
            # Let the CLI handle the same exception raised by the second interrupt.
            raise

        pytest.fail("The second interrupt must not return")

    monkeypatch.setattr(cli.service, "run_config_file", run_config_file)
    assert cli.main(["run", str(tmp_path / "unused.yaml"), "--json"]) == 130
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert len(events) == 1
    assert events[0]["event"] == "run_finished"
    assert events[0]["status"] == "cancelled"
    assert events[0]["exit_code"] == 130
    assert events[0]["forced"] is True
    assert events[0]["manifest"] is None
    assert "Cancellation requested" in captured.err
    assert "Forced cancellation" in captured.err
    assert sigint_handler["handler"] is sigint_handler["previous"]
    assert len(sigint_handler["calls"]) == 2


@pytest.mark.parametrize("entrypoint", ["pipeline", "service"])
def test_explicit_control_and_callback_both_work(
    entrypoint, toy_config, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        pipeline, "run_variable", lambda *args, **kwargs: _small_result()
    )
    events = []
    control = RunControl()
    if entrypoint == "pipeline":
        pipeline.run_pipeline(toy_config, events.append, control=control)
    else:
        service.run_config_file(
            _write_config(tmp_path, toy_config), events.append, control=control
        )
    assert events[0]["event"] == "run_started"
    assert any(event["event"] == "variable_finished" for event in events)


@pytest.mark.parametrize("boundary", ["qa_error", "manifest"])
def test_cancellation_during_saving_keeps_exit_130(
    boundary, toy_config, tmp_path, monkeypatch
):
    from bcflow import writers

    monkeypatch.setattr(
        pipeline, "run_variable", lambda *args, **kwargs: _small_result()
    )
    control = RunControl()
    if boundary == "qa_error":

        def fail_qa(*args, **kwargs):
            control.request_cancel()
            raise OSError("disk full")

        monkeypatch.setattr(writers, "write_qa", fail_qa)
    else:
        original = writers.write_manifest

        def cancel_manifest(*args, **kwargs):
            control.request_cancel()
            return original(*args, **kwargs)

        monkeypatch.setattr(writers, "write_manifest", cancel_manifest)
    summary = service.run_config_file(
        _write_config(tmp_path, toy_config), control=control
    )
    assert summary["exit_code"] == 130
    assert summary["status"] == "cancelled"
    if boundary == "qa_error":
        assert "disk full" in summary["error"]
    else:
        _, manifest = _read_manifest(summary)
        assert manifest["status"] == "cancelled"
        assert all(entry["status"] == "ok" for entry in manifest["variables"].values())


def test_direct_interrupt_while_saving_retries_completed_results(
    toy_config, tmp_path, monkeypatch
):
    from bcflow import writers

    monkeypatch.setattr(
        pipeline, "run_variable", lambda *args, **kwargs: _small_result()
    )
    original = writers.write_result
    calls = []

    def interrupt_once(*args, **kwargs):
        calls.append(kwargs["var_name"])
        if len(calls) == 1:
            raise KeyboardInterrupt()
        return original(*args, **kwargs)

    monkeypatch.setattr(writers, "write_result", interrupt_once)
    summary = service.run_config_file(_write_config(tmp_path, toy_config))
    assert calls == ["tmax", "tmax", "precip"]
    assert summary["exit_code"] == 130
    assert summary["variables"] == {"tmax": "ok", "precip": "ok"}
    run_dir, manifest = _read_manifest(summary)
    assert manifest["status"] == "cancelled"
    assert (run_dir / "tmax" / "scen.nc").is_file()
    assert (run_dir / "precip" / "scen.nc").is_file()


def test_cli_restores_handler_when_service_raises(
    monkeypatch, tmp_path, sigint_handler
):
    def fail(*args, **kwargs):
        assert callable(sigint_handler["handler"])
        raise RuntimeError("service failed")

    monkeypatch.setattr(cli.service, "run_config_file", fail)
    with pytest.raises(RuntimeError, match="service failed"):
        cli.main(["run", str(tmp_path / "unused.yaml"), "--json"])
    assert sigint_handler["handler"] is sigint_handler["previous"]
    assert len(sigint_handler["calls"]) == 2
