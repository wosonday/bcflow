from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from make_toy_data import generate_toy_dataset  # noqa: E402


@pytest.fixture
def toy_paths(tmp_path: Path) -> dict[str, Path]:
    """Sinh dữ liệu toy vào tmp_path, trả về dict đường dẫn từng file/folder."""
    return generate_toy_dataset(tmp_path)


@pytest.fixture
def toy_config(toy_paths: dict[str, Path]) -> dict:
    """Config dict (chưa validate) khớp với toy_paths, dùng đường dẫn tuyệt đối."""
    return {
        "variables": [
            {
                "name": "tasmax",
                "method": "empirical_quantile_mapping",
                "kind": "+",
                "units": "degC",
                "adapt_freq": False,
                "group": {"dim": "time", "prop": "dayofyear", "window": 15},
                "train_params": {"nquantiles": 15},
                "adjust_params": {"interp": "linear", "extrapolation": "constant"},
                "data": {
                    "ref": {
                        "station_layout": "wide",
                        "match_key": "name",
                        "metadata_path": str(toy_paths["tasmax_meta"]),
                        "path": str(toy_paths["tasmax_data"]),
                    },
                    "hist": {"format": "netcdf", "path": str(toy_paths["tasmax_hist"]), "grid_type": "regular_latlon"},
                    "sim": {"format": "netcdf", "path": str(toy_paths["tasmax_sim"]), "grid_type": "regular_latlon"},
                },
            },
            {
                "name": "pr",
                "method": "empirical_quantile_mapping",
                "kind": "*",
                "units": "mm/day",
                "adapt_freq": True,
                "adapt_freq_thresh": "0.5 mm d-1",
                "group": {"dim": "time", "window": 1},
                "train_params": {"nquantiles": 15},
                "adjust_params": {"interp": "linear", "extrapolation": "constant"},
                "data": {
                    "ref": {
                        "station_layout": "long",
                        "match_key": "name",
                        "metadata_path": str(toy_paths["pr_meta"]),
                        "path": str(toy_paths["pr_folder"]),
                    },
                    "hist": {"format": "netcdf", "path": str(toy_paths["pr_hist"]), "grid_type": "regular_latlon"},
                    "sim": {"format": "netcdf", "path": str(toy_paths["pr_sim"]), "grid_type": "regular_latlon"},
                },
            },
        ]
    }
