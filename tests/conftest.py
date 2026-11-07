from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from make_toy_data import generate_toy_dataset  # noqa: E402


@pytest.fixture
def toy_paths(tmp_path: Path) -> dict[str, Path]:
    """Toy data generated into tmp_path; paths keyed by role."""
    return generate_toy_dataset(tmp_path)


@pytest.fixture
def toy_config(toy_paths: dict[str, Path]) -> dict:
    """A raw (not yet validated) config matching toy_paths, with absolute paths.

    The overrides keep the tests fast (15 quantiles, EQM) and exercise both
    groupings: tmax by day of year with a 15-day window, precip ungrouped.
    """
    return {
        "stations": {
            "metadata": str(toy_paths["stations_meta"]),
            "data_dir": str(toy_paths["weather_dir"]),
            "file_pattern": "{id}.csv",
        },
        "variables": ["tmax", "precip"],
        "model": {
            "hist": str(toy_paths["hist_template"]),
            "sim": str(toy_paths["sim_template"]),
            "var_map": {"precip": "pr", "tmax": "tasmax"},
        },
        "correction": {
            "tmax": {
                "method": "empirical_quantile_mapping",
                "group": "dayofyear",
                "window": 15,
                "train_params": {"nquantiles": 15},
            },
            "precip": {
                "method": "empirical_quantile_mapping",
                "group": "none",
                "adapt_freq_thresh": "0.5 mm d-1",
                "train_params": {"nquantiles": 15},
                "adjust_params": {"interp": "linear", "extrapolation": "constant"},
            },
        },
    }


@pytest.fixture
def grid_config(toy_config: dict, toy_paths: dict[str, Path]) -> dict:
    """The toy config with the gridded observations as the reference.

    Units are overridden because the file says "var_unit", as CDO writes.
    """
    cfg = {k: v for k, v in toy_config.items() if k != "stations"}
    cfg["grid"] = {
        "path": str(toy_paths["obs_grid"]),
        "var_map": {"precip": "pr", "tmax": "tasmax"},
        "units": {"precip": "mm d-1", "tmax": "degC"},
        "block_size": 100,
    }
    cfg["periods"] = {"calibration": ["2000", "2012"], "validation": ["2013", "2020"]}
    cfg["correction"] = {
        "tmax": {
            "method": "empirical_quantile_mapping",
            "train_params": {"nquantiles": 15},
        },
        "precip": {
            "method": "empirical_quantile_mapping",
            "train_params": {"nquantiles": 15},
        },
    }
    return cfg
