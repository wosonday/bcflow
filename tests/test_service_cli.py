"""Tests for the JSON command-line interface used by the desktop (GPUI) app.

The contract under test: in --json mode every stdout line is JSON, and the
exit code says success (0) / finished with failures (1) / could not start (2).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

from bcflow.cli import main


def _write_config(tmp_path: Path, cfg: dict) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def _json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_methods_json_lists_the_registry(capsys):
    assert main(["methods", "--json"]) == 0
    names = {m["name"] for m in json.loads(capsys.readouterr().out)}
    assert {"empirical_quantile_mapping", "quantile_delta_mapping"} <= names


def test_schema_describes_the_config(capsys):
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert {"variables", "output"} <= set(schema["properties"])


def test_validate_accepts_a_good_config(toy_config, tmp_path, capsys):
    assert main(["validate", str(_write_config(tmp_path, toy_config)), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"valid": True, "variables": ["tasmax", "pr"], "errors": []}


def test_validate_reports_every_problem_with_its_location(toy_config, tmp_path, capsys):
    bad = copy.deepcopy(toy_config)
    bad["variables"][0]["method"] = "does_not_exist"
    bad["variables"][0]["data"]["hist"]["path"] = str(tmp_path / "missing.nc")
    del bad["variables"][1]["adapt_freq_thresh"]

    assert main(["validate", str(_write_config(tmp_path, bad)), "--json"]) == 1

    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is False
    located = {(e["variable"], tuple(e["loc"])) for e in result["errors"]}
    assert ("tasmax", ("method",)) in located
    assert ("tasmax", ("data", "hist", "path")) in located
    assert any(e["variable"] == "pr" and "adapt_freq_thresh" in e["message"] for e in result["errors"])


def test_run_json_streams_events_then_a_summary(toy_config, tmp_path, capsys):
    cfg = {**toy_config, "output": {"path": str(tmp_path / "out")}}

    assert main(["run", str(_write_config(tmp_path, cfg)), "--json"]) == 0

    events = _json_lines(capsys.readouterr().out)  # fails if any stdout line is not JSON
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert kinds.count("variable_finished") == 2 and "qa" in kinds
    final = events[-1]
    assert final["exit_code"] == 0 and final["status"] == "ok"
    assert final["variables"] == {"tasmax": "ok", "pr": "ok"}
    assert Path(final["manifest"]).is_file()


def test_run_json_reports_an_unreadable_config_as_error(tmp_path, capsys):
    assert main(["run", str(tmp_path / "nope.yaml"), "--json"]) == 2
    final = _json_lines(capsys.readouterr().out)[-1]
    assert final["event"] == "run_finished" and final["status"] == "error"
    assert "not found" in final["error"]


def test_inspect_netcdf_and_station_folder(toy_paths, capsys):
    assert main(["inspect", str(toy_paths["pr_hist"]), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    pr = next(v for v in info["variables"] if v["name"] == "pr")
    assert info["kind"] == "raster" and pr["units"] == "mm/day"
    assert info["time"]["start"] == "2000-01-01"

    assert main(["inspect", str(toy_paths["pr_folder"]), "--json"]) == 0
    folder = json.loads(capsys.readouterr().out)
    assert folder["kind"] == "station_folder" and folder["n_files"] >= 1


def test_inspect_missing_path_exits_2(tmp_path, capsys):
    assert main(["inspect", str(tmp_path / "nothing.nc"), "--json"]) == 2
    assert "error" in json.loads(capsys.readouterr().out)
