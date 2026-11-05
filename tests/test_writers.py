"""Tests for writers.write_run and the CLI wiring: the on-disk layout of a run."""

from __future__ import annotations

import copy
import json
from typing import get_args

import pandas as pd
import xarray as xr
import yaml

from bcflow.cli import main
from bcflow.config_schema import OutputConfig, OutputFormat
from bcflow.pipeline import run_pipeline
from bcflow.writers import _WRITERS, write_run


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_output_formats_in_schema_match_registered_writers():
    """Otherwise a format could pass config validation but have no writer."""
    assert set(get_args(OutputFormat)) == set(_WRITERS)


def test_write_run_writes_every_variable_qa_and_manifest(toy_config, tmp_path):
    out = run_pipeline(toy_config)
    manifest_path = write_run(out, OutputConfig(path=str(tmp_path), formats=["netcdf", "csv"]))
    run_dir = manifest_path.parent

    manifest = _read_json(manifest_path)
    assert {name: v["status"] for name, v in manifest.items()} == {"tasmax": "ok", "pr": "ok"}
    # Relative paths: the run folder stays valid when moved or shared.
    assert manifest["tasmax"]["scen"] == {"netcdf": "tasmax/scen.nc", "csv": "tasmax/scen.csv"}
    assert manifest["pr"]["adj"] == "pr/adj.nc"

    with xr.open_dataset(run_dir / "tasmax" / "scen.nc") as ds:
        assert "tasmax" in ds.data_vars
        assert ds.sizes["station"] == out["results"]["tasmax"]["scen"].sizes["station"]
    assert (run_dir / "pr" / "adj.nc").is_file()

    csv = pd.read_csv(run_dir / "pr" / "scen.csv", index_col="time")
    assert len(csv) == out["results"]["pr"]["scen"].sizes["time"]

    assert len(_read_json(run_dir / "qa.json")) == len(out["qa"])


def test_failed_variable_is_listed_in_manifest_and_others_still_written(toy_config, tmp_path):
    bad = copy.deepcopy(toy_config)
    bad["variables"][0]["method"] = "does_not_exist"
    out = run_pipeline(bad)

    manifest_path = write_run(out, OutputConfig(path=str(tmp_path)))
    manifest = _read_json(manifest_path)

    assert manifest["tasmax"] == {"status": "failed"}
    assert manifest["pr"]["status"] == "ok"
    assert not (manifest_path.parent / "tasmax").exists()
    assert (manifest_path.parent / "pr" / "scen.nc").is_file()


def test_write_failure_is_isolated_and_reported_in_qa(toy_config, tmp_path):
    """tasmax gets an extra dimension, so its CSV cannot be written; pr must
    still be written and the failure must be visible in out['qa']."""
    out = run_pipeline(toy_config)
    out["results"]["tasmax"]["scen"] = out["results"]["tasmax"]["scen"].expand_dims(extra=[0])

    manifest_path = write_run(out, OutputConfig(path=str(tmp_path), formats=["csv"]))
    manifest = _read_json(manifest_path)

    assert manifest["tasmax"] == {"status": "failed"}
    assert manifest["pr"]["status"] == "ok"
    assert any(c.name == "writers:tasmax:exception" and c.status == "fail" for c in out["qa"])


def test_runs_never_overwrite_each_other(tmp_path):
    empty = {"results": {}, "qa": []}
    cfg = OutputConfig(path=str(tmp_path))
    first, second = write_run(empty, cfg), write_run(empty, cfg)  # same second
    assert first.parent != second.parent
    assert first.is_file() and second.is_file()


def test_cli_writes_outputs_relative_to_config_file(toy_config, tmp_path):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    config_file = cfg_dir / "run.yaml"
    config_file.write_text(
        yaml.safe_dump({**toy_config, "output": {"path": "../results", "formats": ["netcdf"]}}),
        encoding="utf-8",
    )

    assert main([str(config_file)]) == 0

    run_dirs = list((tmp_path / "results").iterdir())  # ../results from configs/, not the cwd
    assert len(run_dirs) == 1
    assert _read_json(run_dirs[0] / "manifest.json")["tasmax"]["status"] == "ok"


def test_cli_rejects_invalid_output_section_before_running(toy_config, tmp_path, capsys):
    config_file = tmp_path / "run.yaml"
    config_file.write_text(
        yaml.safe_dump({**toy_config, "output": {"formats": ["excel"]}}), encoding="utf-8"
    )

    assert main([str(config_file)]) == 2
    assert "formats" in capsys.readouterr().err
