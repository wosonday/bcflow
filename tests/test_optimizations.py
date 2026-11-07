"""Cancel file, trimming hist to the reference years, and the reused worker pool."""

from __future__ import annotations

import copy
import os
import signal

import pandas as pd
import pytest
import xarray as xr
import yaml

from bcflow import adjust
from bcflow.cli import main
from bcflow.config_schema import load_config
from bcflow.pipeline import run_pipeline
from bcflow.progress import RunCancelled, RunControl
from bcflow.resources import WorkerPool


def test_cancel_file_cancels_at_the_next_checkpoint(tmp_path):
    flag = tmp_path / "cancel.flag"
    control = RunControl(cancel_file=flag)
    control.check_cancelled()  # no file yet: carries on
    flag.touch()
    with pytest.raises(RunCancelled):
        control.check_cancelled()
    assert control.cancelled


def test_cli_run_with_cancel_file_exits_130(toy_config, tmp_path, capsys):
    flag = tmp_path / "cancel.flag"
    flag.touch()  # the GUI asked to stop before anything ran
    cfg = {**toy_config, "output": {"path": str(tmp_path / "out")}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    assert main(["run", str(path), "--json", "--cancel-file", str(flag)]) == 130


def test_hist_is_cut_to_the_reference_years(toy_config, monkeypatch):
    seen = {}
    extract = adjust.extract_nearest

    def spy(da, ref, qa, **kwargs):
        seen[kwargs["source_label"]] = (
            int(da["time"].dt.year.min()),
            int(da["time"].dt.year.max()),
        )
        return extract(da, ref, qa, **kwargs)

    monkeypatch.setattr(adjust, "extract_nearest", spy)
    cfg = copy.deepcopy(toy_config)
    cfg["variables"] = ["tmax"]
    del cfg["correction"]["precip"]
    path_hist = cfg["model"]["hist"]

    # A model hist reaching further back than the stations (1990 vs 2000).
    source = path_hist.replace("{var}", "tasmax")
    with xr.open_dataset(source) as ds:
        ds = ds.load()
    earlier = ds.isel(time=slice(0, 3650)).assign_coords(
        time=pd.date_range("1990-01-01", periods=3650, freq="D")
    )
    xr.concat([earlier, ds], dim="time").to_netcdf(source)

    out = run_pipeline(cfg)

    assert out["results"]["tmax"] is not None
    first, _last = seen["tmax:hist"]
    assert first >= 2000  # 1990-1999 were never extracted


def test_grid_hist_cut_gives_the_same_result(grid_config, monkeypatch):
    cfg = {
        **grid_config,
        "variables": ["tmax"],
        "correction": {"tmax": grid_config["correction"]["tmax"]},
    }
    cut = adjust.run_variable(load_config(cfg), "tmax", [])["scen"]
    monkeypatch.setattr(adjust, "_reference_years", lambda hist, reference: hist)
    full = adjust.run_variable(load_config(cfg), "tmax", [])["scen"]
    assert bool((cut == full).all())


def _worker_sigint_handler() -> bool:
    return signal.getsignal(signal.SIGINT) == signal.SIG_IGN


def _worker_threads() -> str | None:
    return os.environ.get("OMP_NUM_THREADS")


def test_worker_pool_is_reused_grows_and_workers_ignore_ctrl_c():
    with WorkerPool() as pool:
        first = pool.get(1)
        assert pool.get(1) is first  # same pool for the next batch
        assert first.submit(_worker_sigint_handler).result(timeout=60)
        assert first.submit(_worker_threads).result(timeout=60) == "1"
        grown = pool.get(2)
        assert grown is not first and pool.size == 2
    assert pool.size == 0
