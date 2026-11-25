"""Tests for the JSON command-line interface used by the desktop (GPUI) app.

The contract under test: in --json mode every stdout line is JSON, and the
exit code says success (0) / finished with failures (1) / could not start (2).
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml
from make_toy_data import generate_toy_dataset

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
    assert {"stations", "variables", "model", "correction", "output"} <= set(
        schema["properties"]
    )


def test_validate_accepts_a_good_config(toy_config, tmp_path, capsys):
    assert main(["validate", str(_write_config(tmp_path, toy_config)), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True and result["errors"] == []
    assert result["variables"] == ["tmax", "precip"]
    # toy stations are complete, but their 21-year record is shorter than 30 years
    assert {w["name"] for w in result["warnings"]} == {
        "coverage:tmax:short_period",
        "coverage:precip:short_period",
    }


def test_validate_reports_schema_errors_with_their_location(
    toy_config, tmp_path, capsys
):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["tmax"]["window"] = 0
    bad["correction"]["precip"]["group"] = "week"

    assert main(["validate", str(_write_config(tmp_path, bad)), "--json"]) == 1

    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is False
    located = {(e["variable"], tuple(e["loc"])) for e in result["errors"]}
    assert ("tmax", ("correction", "tmax", "window")) in located
    assert ("precip", ("correction", "precip", "group")) in located


def test_validate_reports_every_missing_input_and_bad_method(
    toy_config, tmp_path, capsys
):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["tmax"]["method"] = "does_not_exist"
    bad["model"]["sim"] = str(tmp_path / "missing_{var}.nc")
    bad["stations"]["file_pattern"] = "{name}.csv"  # toy files are named by id

    assert main(["validate", str(_write_config(tmp_path, bad)), "--json"]) == 1

    located = {
        (e["variable"], tuple(e["loc"]))
        for e in json.loads(capsys.readouterr().out)["errors"]
    }
    assert ("tmax", ("correction", "tmax", "method")) in located
    assert ("tmax", ("model", "sim")) in located and (
        "precip",
        ("model", "sim"),
    ) in located
    assert (None, ("stations", "file_pattern")) in located


def _errors_by_loc(result: dict) -> dict[tuple, str]:
    return {(e["variable"], tuple(e["loc"])): e["message"] for e in result["errors"]}


def test_validate_reports_post_only_and_multivariate_primary_methods(
    toy_config, tmp_path, capsys
):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["tmax"]["method"] = "extreme_values"  # family == "post"
    bad["correction"]["precip"]["method"] = "mbcn"  # min_vars == 2

    assert main(["validate", str(_write_config(tmp_path, bad)), "--json"]) == 1

    messages = _errors_by_loc(json.loads(capsys.readouterr().out))
    post = messages[("tmax", ("correction", "tmax", "method"))]
    assert "post" in post and "'post:'" in post
    multi = messages[("precip", ("correction", "precip", "method"))]
    assert "at least 2 variables" in multi and "'targets:'" in multi


def test_validate_reports_train_params_on_adjust_only_method(
    toy_config, tmp_path, capsys
):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["tmax"]["method"] = "npdf_transform"  # family == "adjust_only"
    bad["correction"]["tmax"]["train_params"] = {"nquantiles": 15}

    assert main(["validate", str(_write_config(tmp_path, bad)), "--json"]) == 1

    messages = _errors_by_loc(json.loads(capsys.readouterr().out))
    message = messages[("tmax", ("correction", "tmax", "train_params"))]
    assert "adjust_only" in message and "train_params" in message
    # The method-level min_vars error is collected too, not just the first one.
    assert ("tmax", ("correction", "tmax", "method")) in messages


@pytest.mark.skipif(
    importlib.util.find_spec("ot") is not None,
    reason="the optional POT package is installed",
)
def test_validate_reports_missing_pot_with_the_other_input_errors(
    toy_config, tmp_path, capsys
):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["tmax"]["method"] = "otc"  # needs POT
    bad["model"]["sim"] = str(tmp_path / "missing_{var}.nc")

    assert main(["validate", str(_write_config(tmp_path, bad)), "--json"]) == 1

    messages = _errors_by_loc(json.loads(capsys.readouterr().out))
    pot = messages[("tmax", ("correction", "tmax", "method"))]
    assert "POT" in pot and "pip install POT" in pot
    assert ("tmax", ("model", "sim")) in messages
    assert ("precip", ("model", "sim")) in messages


def test_validate_reports_invalid_post_steps(toy_config, tmp_path, capsys):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["precip"]["post"] = [
        {"method": "scaling"},  # family == "train_adjust"
        {"method": "does_not_exist"},
    ]

    assert main(["validate", str(_write_config(tmp_path, bad)), "--json"]) == 1

    messages = _errors_by_loc(json.loads(capsys.readouterr().out))
    family = messages[("precip", ("correction", "precip", "post", 0, "method"))]
    assert "family 'post'" in family and "train_adjust" in family
    unknown = messages[("precip", ("correction", "precip", "post", 1, "method"))]
    assert "not registered" in unknown


def test_validate_accepts_season_post_and_custom_variables(
    toy_config, toy_paths, tmp_path, capsys
):
    import pandas as pd

    for path in sorted(toy_paths["weather_dir"].glob("*.csv")):
        df = pd.read_csv(path)
        df["cloud"] = 0.5
        df.to_csv(path, index=False)
    for key in ("hist_template", "sim_template"):
        Path(str(toy_paths[key]).replace("{var}", "clt")).touch()

    cfg = copy.deepcopy(toy_config)
    cfg["variables"].append("cloud")
    cfg["model"]["var_map"]["cloud"] = "clt"
    cfg["custom_variables"] = {
        "cloud": {"units": "fraction", "kind": "*", "bounds": [0.0, 1.0]}
    }
    cfg["correction"]["tmax"]["group"] = "season"
    cfg["correction"]["tmax"].pop("window", None)
    cfg["correction"]["precip"]["post"] = [{"method": "extreme_values"}]

    assert main(["validate", str(_write_config(tmp_path, cfg)), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True and result["errors"] == []
    assert set(result["variables"]) == {"tmax", "precip", "cloud"}


def test_validate_names_a_custom_variable_in_schema_errors(
    toy_config, tmp_path, capsys
):
    bad = copy.deepcopy(toy_config)
    bad["custom_variables"] = {
        "cloud": {"units": "fraction", "kind": "*"}  # '*' needs a lower bound
    }

    assert main(["validate", str(_write_config(tmp_path, bad)), "--json"]) == 1

    error = next(
        e
        for e in json.loads(capsys.readouterr().out)["errors"]
        if e["loc"][:1] == ["custom_variables"]
    )
    assert error["variable"] == "cloud"
    assert error["loc"][:2] == ["custom_variables", "cloud"]


def test_run_json_streams_events_then_a_summary(toy_config, tmp_path, capsys):
    cfg = {**toy_config, "output": {"path": str(tmp_path / "out")}}

    assert main(["run", str(_write_config(tmp_path, cfg)), "--json"]) == 0

    events = _json_lines(
        capsys.readouterr().out
    )  # fails if any stdout line is not JSON
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert kinds.count("variable_finished") == 2 and "qa" in kinds
    final = events[-1]
    assert final["exit_code"] == 0 and final["status"] == "ok"
    assert final["variables"] == {"tmax": "ok", "precip": "ok"}
    assert Path(final["manifest"]).is_file()


def test_run_json_reports_an_unreadable_config_as_error(tmp_path, capsys):
    assert main(["run", str(tmp_path / "nope.yaml"), "--json"]) == 2
    final = _json_lines(capsys.readouterr().out)[-1]
    assert final["event"] == "run_finished" and final["status"] == "error"
    assert "not found" in final["error"]


def test_inspect_netcdf_station_folder_and_station_file(toy_paths, capsys):
    assert main(["inspect", str(toy_paths["hist_pr"]), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    pr = next(v for v in info["variables"] if v["name"] == "pr")
    assert info["kind"] == "raster" and pr["units"] == "mm/day"
    assert info["time"]["start"] == "2000-01-01"

    assert main(["inspect", str(toy_paths["weather_dir"]), "--json"]) == 0
    folder = json.loads(capsys.readouterr().out)
    assert folder["kind"] == "station_folder" and folder["stations"] == [
        "ST01",
        "ST02",
        "ST03",
    ]

    assert main(["inspect", str(toy_paths["weather_dir"] / "ST01.csv"), "--json"]) == 0
    table = json.loads(capsys.readouterr().out)
    assert (
        table["columns"] == ["date", "precip", "tmax"]
        and table["time"]["start"] == "2000-01-01"
    )


def test_inspect_missing_path_exits_2(tmp_path, capsys):
    assert main(["inspect", str(tmp_path / "nothing.nc"), "--json"]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    "name", ["example_toy.yaml", "example_toy.json", "example_grid.yaml"]
)
def test_example_configs_are_valid(name, tmp_path, capsys):
    """The shipped examples must validate against the toy data, laid out as in
    the repository: configs/ next to data/toy/."""
    generate_toy_dataset(tmp_path / "data" / "toy")
    (tmp_path / "configs").mkdir()
    source = Path(__file__).resolve().parent.parent / "configs" / name
    target = tmp_path / "configs" / name
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    assert main(["validate", str(target), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_json_and_yaml_configs_are_the_same():
    root = Path(__file__).resolve().parent.parent / "configs"
    as_yaml = yaml.safe_load((root / "example_toy.yaml").read_text(encoding="utf-8"))
    as_json = json.loads((root / "example_toy.json").read_text(encoding="utf-8"))
    assert as_yaml == as_json


def test_unsupported_config_suffix_is_rejected(toy_config, tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text("x = 1", encoding="utf-8")
    assert main(["validate", str(path), "--json"]) == 1
    assert (
        "Unsupported config format"
        in json.loads(capsys.readouterr().out)["errors"][0]["message"]
    )


@pytest.mark.parametrize(
    ("method", "group"),
    [
        ("mbcn", "none"),
        ("principal_components", "month"),
        ("npdf_transform", "none"),
    ],
)
def test_validate_accepts_multivariate_targets(
    toy_config, tmp_path, capsys, method, group
):
    cfg = copy.deepcopy(toy_config)
    cfg.pop("correction", None)
    cfg["targets"] = [
        {
            "name": "joint",
            "variables": ["tmax", "precip"],
            "method": method,
            "group": group,
        }
    ]

    assert main(["validate", str(_write_config(tmp_path, cfg)), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True and result["errors"] == []


def test_validate_reports_target_semantic_errors(toy_config, tmp_path, capsys):
    bad = copy.deepcopy(toy_config)
    bad.pop("correction", None)
    bad["targets"] = [
        {
            "name": "t_post",
            "variables": ["tmax"],
            "method": "extreme_values",  # family == "post"
            "post": [
                {"method": "scaling"},  # family != "post"
                {"method": "does_not_exist"},
            ],
        },
        {
            "name": "t_adjust_only",
            "variables": ["precip"],
            "method": "npdf_transform",  # min_vars == 2 & adjust_only
            "train_params": {"n_iter": 5},
        },
    ]

    assert main(["validate", str(_write_config(tmp_path, bad)), "--json"]) == 1
    messages = _errors_by_loc(json.loads(capsys.readouterr().out))

    assert "family 'post'" in messages[("tmax", ("targets", 0, "method"))]
    assert "family 'post'" in messages[("tmax", ("targets", 0, "post", 0, "method"))]
    assert "not registered" in messages[("tmax", ("targets", 0, "post", 1, "method"))]
    assert "at least 2 variables" in messages[("precip", ("targets", 1, "method"))]
    assert "adjust_only" in messages[("precip", ("targets", 1, "train_params"))]

    bad_unknown = copy.deepcopy(toy_config)
    bad_unknown.pop("correction", None)
    bad_unknown["targets"] = [
        {"name": "joint", "variables": ["tmax", "precip"], "method": "does_not_exist"}
    ]
    assert main(["validate", str(_write_config(tmp_path, bad_unknown)), "--json"]) == 1
    unknown_msgs = _errors_by_loc(json.loads(capsys.readouterr().out))
    assert "not registered" in unknown_msgs[(None, ("targets", 0, "method"))]


def test_validate_reports_otc_non_projection_and_missing_pot_on_targets(
    toy_config, tmp_path, capsys, monkeypatch
):
    bad = copy.deepcopy(toy_config)
    bad.pop("correction", None)
    bad["targets"] = [
        {
            "name": "joint_otc",
            "variables": ["tmax", "precip"],
            "method": "otc",
            "group": "none",
        }
    ]

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *args, **kwargs: (
            None if name == "ot" else real_find_spec(name, *args, **kwargs)
        ),
    )

    assert main(["validate", str(_write_config(tmp_path, bad)), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    target_msgs = [
        e["message"] for e in result["errors"] if e["loc"] == ["targets", 0, "method"]
    ]
    assert any("does not support projection" in m for m in target_msgs)
    assert any("POT" in m and "pip install POT" in m for m in target_msgs)
