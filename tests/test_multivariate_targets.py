"""Real multivariate targets through orchestration, disk output, and UI readers."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from make_toy_data import generate_toy_dataset

from app.services.results import (
    artifact_path,
    declared_artifacts,
    load_manifest,
    read_evaluation,
    read_qa,
)
from bcflow import adjust, pipeline
from bcflow.config_schema import OutputConfig
from bcflow.progress import RunControl
from bcflow.resources import WorkerSelection
from bcflow.writers import write_run


@pytest.fixture
def target_config(tmp_path):
    paths = generate_toy_dataset(tmp_path / "inputs", n_years=4)
    return {
        "stations": {
            "metadata": str(paths["stations_meta"]),
            "data_dir": str(paths["weather_dir"]),
        },
        "variables": ["tmax", "precip"],
        "model": {
            "hist": str(paths["hist_template"]),
            "sim": str(paths["sim_template"]),
            "var_map": {"tmax": "tasmax", "precip": "pr"},
        },
        # Both complete non-leap years have 365 days, as required by MBCn.
        "periods": {"calibration": ["2001", "2001"], "validation": ["2002", "2002"]},
        "targets": [
            {
                "name": "joint",
                "variables": ["tmax", "precip"],
                "method": "principal_components",
                "group": "month",
            }
        ],
    }


def _write_and_read(out, config, folder):
    path = write_run(out, OutputConfig(path=str(folder)), config=config)
    manifest = load_manifest(path)
    assert manifest["schema_version"] == 1
    assert read_qa(path) == json.loads(
        json.dumps([check.as_dict() for check in out["qa"]], default=str)
    )
    for _, relative in declared_artifacts(manifest):
        assert artifact_path(path, relative).is_file()
    assert {
        name: entry["status"] for name, entry in manifest["variables"].items()
    } == out["variable_statuses"]
    for name, result in out["results"].items():
        entry = manifest["variables"][name]
        if result is None:
            assert entry == {"status": "failed"}
            continue
        assert entry["scen"] == {"netcdf": f"{name}/scen.nc"}
        assert entry["hist_eval"] == {"netcdf": f"{name}/hist_eval.nc"}
        assert entry["evaluation"] == f"{name}/evaluation.csv"
        evaluation = read_evaluation(path, name)
        assert not evaluation.empty
        assert set(evaluation["period"]) == {"calibration", "validation"}
        np.testing.assert_allclose(
            evaluation[["raw", "corrected"]], result["evaluation"][["raw", "corrected"]]
        )
        assert np.isfinite(evaluation.select_dtypes("number").to_numpy()).all()
        with xr.open_dataset(path.parent / entry["scen"]["netcdf"]) as ds:
            if result["grid"] is None:
                values = ds[name].transpose("time", "station").values
            else:
                grid = result["grid"]
                values = (
                    ds[name].transpose("time", "lat", "lon").values[:, grid.iy, grid.ix]
                )
            np.testing.assert_allclose(
                values, result["scen"].transpose("time", "station"), rtol=1e-6
            )
        with xr.open_dataset(path.parent / entry["hist_eval"]["netcdf"]) as ds:
            assert {"obs", "raw", "corrected"} <= set(ds.data_vars)
            if result["grid"] is None:
                assert "period" in ds.coords
            else:
                assert "period" in ds.data_vars
            assert ds["period"].dims == ("time",)
            assert set(ds["period"].values) == {"calibration", "validation"}
        adj_path = path.parent / name / "adj.nc"
        if result["adj"] is None:
            assert entry["adj"] is None
            assert not adj_path.exists()
        else:
            assert entry["adj"] == f"{name}/adj.nc"
            with xr.open_dataset(adj_path) as ds:
                assert ds.data_vars
                xr.testing.assert_allclose(ds, result["adj"].ds)
    for name, status in out["variable_statuses"].items():
        if status in ("cancelled", "skipped"):
            assert manifest["variables"][name] == {"status": status}
            assert not (path.parent / name).exists()
    return manifest


@pytest.mark.parametrize("method", ["principal_components", "mbcn", "npdf_transform"])
@pytest.mark.parametrize("reference", ["stations", "grid"])
def test_multivariate_pipeline_and_outputs(
    target_config, tmp_path, monkeypatch, method, reference
):
    cfg = copy.deepcopy(target_config)
    target = cfg["targets"][0]
    target["method"] = method
    if method == "mbcn":
        target.update(
            group="none", train_params={"n_iter": 2, "base_kws": {"nquantiles": 10}}
        )
    elif method == "npdf_transform":
        target.update(
            group="none", adjust_params={"n_iter": 2, "base_kws": {"nquantiles": 10}}
        )
    regrids = {"tmax": set(), "precip": set()}
    if reference == "grid":
        stations = cfg.pop("stations")
        cfg["grid"] = {
            "path": str(Path(stations["data_dir"]).parent / "obs_grid.nc"),
            "var_map": {"tmax": "tasmax", "precip": "pr"},
            "units": {"tmax": "degC", "precip": "mm d-1"},
            "region": {"lat": [15, 16], "lon": [105, 107]},
            "block_size": 2,
            "workers": 1,
        }
        real_regrid = adjust.regrid_to_points

        def spy_regrid(da, grid, qa, **kwargs):
            regrids[kwargs["source_label"].split(":")[0]].add(kwargs["method"])
            return real_regrid(da, grid, qa, **kwargs)

        def select(requested, fraction, remaining, estimated):
            # Ensure the requested process path runs even on a memory-limited CI host.
            return WorkerSelection(
                workers=min(requested, remaining),
                cpu_limit=8,
                memory_limit=remaining,
                available_bytes=remaining * estimated * 2,
                budget_bytes=remaining * estimated,
                estimated_task_bytes=estimated,
                over_budget=False,
            )

        monkeypatch.setattr(adjust, "regrid_to_points", spy_regrid)
        monkeypatch.setattr(adjust, "select_workers", select)

    outputs = []
    for workers in (1, 2) if reference == "grid" else (1,):
        if reference == "grid":
            cfg["grid"]["workers"] = workers
        events = []
        out = pipeline.run_pipeline(cfg, events.append)
        assert not [c.as_dict() for c in out["qa"] if c.status == "fail"]
        assert out["variable_statuses"] == {"tmax": "ok", "precip": "ok"}
        assert out["cancelled"] is False
        assert [
            {key: value for key, value in e.items() if key != "elapsed_seconds"}
            for e in events
            if e["event"] == "variable_started"
        ] == [
            {"event": "variable_started", "variable": v, "index": i, "total": 2}
            for i, v in enumerate(("tmax", "precip"), 1)
        ]
        assert [e["variable"] for e in events if e["event"] == "variable_finished"] == [
            "tmax",
            "precip",
        ]
        qa_events = [e for e in events if e["event"] == "qa"]
        assert [e["check"] for e in qa_events] == [c.as_dict() for c in out["qa"]]
        assert all(
            e["variable"] == (e["check"]["context"].get("variable") or "joint")
            for e in qa_events
        )
        if reference == "grid":
            starts = [e for e in events if e.get("stage") == "block_started"]
            assert [e["block_cells"] for e in starts] == [2, 2, 2, 2]
            selections = [
                e["workers"] for e in events if e.get("stage") == "worker_selection"
            ]
            assert max(selections) == workers
        for result in out["results"].values():
            assert bool(np.isfinite(result["scen"]).all())
            assert np.isfinite(
                result["evaluation"].select_dtypes("number").to_numpy()
            ).all()
            assert (result["adj"] is None) is (method == "npdf_transform")
        assert float(out["results"]["precip"]["scen"].min()) >= 0
        manifest = _write_and_read(out, cfg, tmp_path / f"outputs_{workers}")
        assert manifest["status"] == "ok"
        outputs.append(out)

    if reference == "grid":
        assert regrids == {"tmax": {"bilinear"}, "precip": {"conservative"}}
        sequential, parallel = outputs
        for name in cfg["variables"]:
            seq, par = sequential["results"][name], parallel["results"][name]
            xr.testing.assert_identical(seq["scen"].coords, par["scen"].coords)
            xr.testing.assert_allclose(seq["scen"], par["scen"])
            xr.testing.assert_allclose(seq["hist_eval"], par["hist_eval"])
            pd.testing.assert_frame_equal(seq["evaluation"], par["evaluation"])
            if seq["adj"] is not None:
                xr.testing.assert_allclose(seq["adj"].ds, par["adj"].ds)


def test_mbcn_unequal_validation_length_warns(target_config):
    cfg = copy.deepcopy(target_config)
    cfg["targets"][0].update(
        method="mbcn",
        group="none",
        train_params={"n_iter": 2, "base_kws": {"nquantiles": 10}},
    )
    cfg["periods"]["validation"] = ["2002-01-01", "2002-06-30"]
    out = pipeline.run_pipeline(cfg)
    assert out["variable_statuses"] == {"tmax": "ok", "precip": "ok"}
    assert not [c for c in out["qa"] if c.status == "fail"]
    for name, result in out["results"].items():
        assert set(result["evaluation"]["period"]) == {"calibration"}
        assert set(result["hist_eval"]["period"].values) == {"calibration"}
        warning = next(
            c for c in out["qa"] if c.name == f"evaluation:{name}:validation:skipped"
        )
        assert warning.status == "warn"
        assert warning.context == {"variable": name, "period_label": "validation"}
        # xsdba 0.7 reports the shorter validation array as an indexing error.
        assert warning.message == (
            f"Evaluation of the validation period skipped for '{name}': "
            "index 181 is out of bounds for axis 1 with size 181"
        )


def test_station_intersection_warns_and_writes_common_stations(target_config, tmp_path):
    station = Path(target_config["stations"]["data_dir"]) / "ST02.csv"
    pd.read_csv(station).drop(columns=["precip"]).to_csv(station, index=False)
    out = pipeline.run_pipeline(target_config)
    assert not [c for c in out["qa"] if c.status == "fail"]
    warning = next(
        c for c in out["qa"] if c.name == "target:joint:station_intersection"
    )
    assert warning.status == "warn"
    assert warning.context["dropped_stations"] == ["ST02"]
    for result in out["results"].values():
        assert list(result["scen"].station.values) == ["ST01", "ST03"]
        assert bool(np.isfinite(result["scen"]).all())
    _write_and_read(out, target_config, tmp_path / "outputs")


def _add_tmin(cfg):
    cfg["variables"].append("tmin")
    cfg["model"]["var_map"]["tmin"] = "tasmax"
    for path in Path(cfg["stations"]["data_dir"]).glob("*.csv"):
        data = pd.read_csv(path)
        data["tmin"] = data["tmax"] - 5
        data.to_csv(path, index=False)


def test_failed_target_isolates_all_its_variables(target_config, tmp_path):
    cfg = copy.deepcopy(target_config)
    _add_tmin(cfg)
    cfg["targets"][0].update(name="bad", method="no_such_method")
    cfg["targets"].append({"name": "good", "variables": ["tmin"], "method": "scaling"})
    events = []
    out = pipeline.run_pipeline(cfg, events.append)
    assert out["variable_statuses"] == {
        "tmax": "failed",
        "precip": "failed",
        "tmin": "ok",
    }
    assert out["results"]["tmax"] is out["results"]["precip"] is None
    error = next(c for c in out["qa"] if c.name == "pipeline:bad:exception")
    assert error.status == "fail"
    assert error.message.startswith("Target 'bad' failed: ")
    assert error.context == {"target": "bad", "variables": ["tmax", "precip"]}
    assert [e["variable"] for e in events if e["event"] == "variable_finished"] == [
        "tmax",
        "precip",
        "tmin",
    ]
    manifest = _write_and_read(out, cfg, tmp_path / "outputs")
    assert manifest["status"] == "failed"


@pytest.mark.parametrize("stage", ["variable_started", "training", "adjustment"])
def test_cancel_multivariate_target_retains_completed_and_skips_later(
    target_config, tmp_path, stage
):
    cfg = copy.deepcopy(target_config)
    _add_tmin(cfg)
    cfg["variables"] = ["tmin", "tmax", "precip", "wind"]
    cfg["model"]["var_map"]["wind"] = "pr"
    cfg["targets"] = [
        {"name": "completed", "variables": ["tmin"], "method": "scaling"},
        cfg["targets"][0],
        {"name": "later", "variables": ["wind"], "method": "scaling"},
    ]
    events = []

    def on_event(event):
        events.append(event)
        if (event["event"] == stage and event.get("variable") == "tmax") or (
            event.get("stage") == stage and event.get("variable") == "joint"
        ):
            control.request_cancel()

    control = RunControl(on_event)
    out = pipeline.run_pipeline(cfg, control=control)
    assert out["cancelled"] is True
    assert out["variable_statuses"] == {
        "tmin": "ok",
        "tmax": "cancelled",
        "precip": "cancelled",
        "wind": "skipped",
    }
    assert set(out["results"]) == {"tmin"}
    assert not [c for c in out["qa"] if c.status == "fail"]
    assert [e["check"] for e in events if e["event"] == "qa"] == [
        c.as_dict() for c in out["qa"]
    ]
    assert [
        (e["variable"], e["status"])
        for e in events
        if e["event"] == "variable_finished"
    ] == [
        ("tmin", "ok"),
        ("tmax", "cancelled"),
        ("precip", "cancelled"),
    ]
    assert [e["index"] for e in events if e["event"] == "variable_started"] == [1, 2, 3]
    assert events[-1]["event"] == "run_cancelled"
    assert events[-1]["variable"] == "tmax"
    manifest = _write_and_read(out, cfg, tmp_path / "outputs")
    assert manifest["status"] == "cancelled"


@pytest.fixture
def joint_grid_inputs():
    time = pd.date_range("2001-01-01", "2001-12-31")
    rng = np.random.default_rng(42)
    fields = {
        name: xr.DataArray(
            offset + rng.random((len(time), 2, 2)),
            dims=("time", "lat", "lon"),
            coords={"time": time, "lat": [10.0, 20.0], "lon": [100.0, 110.0]},
            attrs={"units": units},
        )
        for name, offset, units in (("tmax", 25.0, "degC"), ("precip", 100.0, "mm d-1"))
    }
    fields["precip"][0] = [[100.0, 101.0], [200.0, 201.0]]
    references = {name: field.copy(deep=True) for name, field in fields.items()}
    references["tmax"][:, 0, 1] = np.nan
    references["precip"][:, 1, 1] = np.nan
    # Geographic coordinates deliberately differ from the dimension indexes.
    references = {
        name: field.rename(lat="y", lon="x").assign_coords(
            latitude=("y", field.lat.values),
            longitude=("x", field.lon.values),
            y=[0, 1],
            x=[0, 1],
        )
        for name, field in references.items()
    }
    cfg = {
        "variables": ["tmax", "precip"],
        "grid": {
            "path": "unused.nc",
            "workers": 1,
            "block_size": 1,
            "var_map": {"tmax": "tmax", "precip": "precip"},
        },
        "model": {
            "hist": "unused.nc",
            "sim": "unused.nc",
            "var_map": {"tmax": "tmax", "precip": "precip"},
        },
        "periods": {"calibration": ["2001", "2001"]},
        "targets": [
            {
                "name": "joint",
                "variables": ["tmax", "precip"],
                "method": "principal_components",
                "group": "none",
            }
        ],
    }
    return cfg, references, fields


@pytest.mark.parametrize("reverse_dim", ["y", "x"])
@pytest.mark.parametrize("reverse_variable", ["tmax", "precip"])
@pytest.mark.parametrize("cached", [True, False])
def test_multivariate_grid_reversed_axes_keep_values_at_true_coordinates(
    joint_grid_inputs, monkeypatch, reverse_dim, reverse_variable, cached
):
    cfg, references, fields = joint_grid_inputs
    references[reverse_variable] = references[reverse_variable].isel(
        {reverse_dim: slice(None, None, -1)}
    )
    monkeypatch.setattr(
        adjust,
        "open_grid_reference",
        lambda grid, variable, qa: (references[variable], "latitude", "longitude"),
    )
    monkeypatch.setattr(
        adjust,
        "load_model_variable",
        lambda model, kind, variable, qa: fields[variable],
    )
    if not cached:
        monkeypatch.setattr(adjust, "read_grid_cells", lambda *args, **kwargs: None)

    out = pipeline.run_pipeline(cfg)
    assert out["variable_statuses"] == {"tmax": "ok", "precip": "ok"}
    assert not [c.as_dict() for c in out["qa"] if c.status == "fail"]
    precip = out["results"]["precip"]
    obs = precip["hist_eval"]["obs"]
    assert float(obs.sel(station="10.0000_100.0000").isel(time=0)) == 100.0
    assert float(obs.sel(station="20.0000_100.0000").isel(time=0)) == 200.0
    for name, result in out["results"].items():
        grid = result["grid"]
        np.testing.assert_array_equal(grid.lat, [10.0, 20.0])
        np.testing.assert_array_equal(grid.lon, [100.0, 110.0])
        assert grid.ids == ["10.0000_100.0000", "20.0000_100.0000"]
        for lat in (10.0, 20.0):
            np.testing.assert_allclose(
                result["hist_eval"]["obs"].sel(station=f"{lat:.4f}_100.0000"),
                fields[name].sel(lat=lat, lon=100.0),
            )


@pytest.mark.parametrize("axis", ["latitude", "longitude"])
@pytest.mark.parametrize("mismatch", ["shifted", "shorter"])
def test_multivariate_grid_axis_mismatch_fails_before_model_loading(
    joint_grid_inputs, monkeypatch, axis, mismatch
):
    from bcflow.config_schema import load_config

    raw, references, _ = joint_grid_inputs
    ref = references["precip"]
    dim = ref[axis].dims[0]
    references["precip"] = (
        ref.assign_coords({axis: ref[axis] + 1.0})
        if mismatch == "shifted"
        else ref.isel({dim: [0]})
    )
    monkeypatch.setattr(
        adjust,
        "open_grid_reference",
        lambda grid, variable, qa: (references[variable], "latitude", "longitude"),
    )

    def unexpected_model_load(*args, **kwargs):
        raise AssertionError("Mismatched reference axes must fail before model loading")

    monkeypatch.setattr(adjust, "load_model_variable", unexpected_model_load)
    cfg = load_config(raw)
    qa = []
    with pytest.raises(ValueError, match="axes_mismatch"):
        adjust.run_target(cfg, cfg.resolved_targets()[0], qa)
    check = next(c for c in qa if c.name == "grid:joint:axes_mismatch")
    assert check.status == "fail"
    assert "tmax" in check.message and "precip" in check.message
    assert check.context["variables"] == ["tmax", "precip"]


def test_multivariate_grid_coordinate_roundoff_preserves_reference_coordinates(
    joint_grid_inputs, monkeypatch
):
    cfg, references, fields = joint_grid_inputs
    ref = references["precip"]
    references["precip"] = ref.assign_coords(
        latitude=ref.latitude + 5e-7, longitude=ref.longitude + 5e-7
    )
    monkeypatch.setattr(
        adjust,
        "open_grid_reference",
        lambda grid, variable, qa: (references[variable], "latitude", "longitude"),
    )
    monkeypatch.setattr(
        adjust,
        "load_model_variable",
        lambda model, kind, variable, qa: fields[variable],
    )
    out = pipeline.run_pipeline(cfg)
    assert out["variable_statuses"] == {"tmax": "ok", "precip": "ok"}
    assert not [c.as_dict() for c in out["qa"] if c.status == "fail"]
    result = out["results"]["precip"]
    np.testing.assert_array_equal(result["grid"].lat, [10.0, 20.0])
    np.testing.assert_array_equal(result["grid"].lon, [100.0, 110.0])
    assert (
        float(result["hist_eval"]["obs"].sel(station="10.0000_100.0000").isel(time=0))
        == 100.0
    )
