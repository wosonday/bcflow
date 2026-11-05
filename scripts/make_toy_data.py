"""Sinh dữ liệu toy cho 2 tình huống thực tế: tasmax (wide CSV, kind="+")
và pr (long CSV, kind="*", zero-inflated -> cần adapt_freq).

generate_toy_dataset(root) có thể gọi từ CLI (sinh vào data/) hoặc từ
pytest fixture (sinh vào tmp_path) — cùng một logic, không viết trùng.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

STATIONS = pd.DataFrame({
    "id": ["ST01", "ST02", "ST03"],
    "name": ["hanoi", "danang", "hcmc"],
    "lat": [21.03, 16.05, 10.78],
    "lon": [105.85, 108.21, 106.70],
})


def generate_toy_dataset(root: Path, seed: int = 42, n_years: int = 21) -> dict[str, Path]:
    """Sinh toàn bộ file toy dưới `root`, trả về dict đường dẫn các file/folder đã tạo."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    time = pd.date_range("2000-01-01", periods=365 * n_years, freq="D")
    n = len(time)
    doy = time.dayofyear.values

    paths: dict[str, Path] = {}

    # --- tasmax: ref dạng wide CSV ---
    tasmax_meta = root / "tasmax_stations_meta.csv"
    tasmax_data = root / "tasmax_ref_wide.csv"
    STATIONS.to_csv(tasmax_meta, index=False)
    cols = {"time": time}
    for i, name in enumerate(STATIONS["name"]):
        seasonal = -10 * np.cos(2 * np.pi * doy / 365) + 27 - 2 * i
        cols[name] = seasonal + rng.normal(0, 1.2, size=n)
    pd.DataFrame(cols).to_csv(tasmax_data, index=False)
    paths["tasmax_meta"] = tasmax_meta
    paths["tasmax_data"] = tasmax_data

    # --- pr: ref dạng long CSV (mỗi file 1 trạm) ---
    pr_meta = root / "pr_stations_meta.csv"
    pr_folder = root / "pr_ref_long"
    pr_folder.mkdir(exist_ok=True)
    STATIONS.to_csv(pr_meta, index=False)
    for i, name in enumerate(STATIONS["name"]):
        wet_season = np.clip(np.sin(2 * np.pi * (doy - 150) / 365), 0, None)
        rain_prob = 0.15 + 0.5 * wet_season
        is_wet = rng.random(n) < rain_prob
        amount = rng.gamma(shape=1.5, scale=8.0, size=n)
        pr = np.where(is_wet, amount, 0.0)
        pd.DataFrame({"time": time, "value": pr}).to_csv(pr_folder / f"{name}.csv", index=False)
    paths["pr_meta"] = pr_meta
    paths["pr_folder"] = pr_folder

    # --- raster hist/sim, có bias cố ý so với ref để kiểm chứng việc hiệu chỉnh ---
    lats = np.linspace(9.0, 22.5, 6)
    lons = np.linspace(104.5, 109.5, 6)
    doy_da = xr.DataArray(doy, dims="time", coords={"time": time})

    def build(kind: str, bias_add: float, bias_mult: float) -> xr.Dataset:
        seasonal = -10 * np.cos(2 * np.pi * doy_da / 365) + 27
        base = seasonal.values[:, None, None] + rng.normal(0, 1.2, size=(n, len(lats), len(lons)))
        var_name = "tasmax" if kind == "+" else "pr"
        if kind == "+":
            field = base + bias_add
            units = "degC"
        else:
            wet_season = np.clip(np.sin(2 * np.pi * (doy_da.values - 150) / 365), 0, None)
            rain_prob = (0.25 + 0.5 * wet_season)[:, None, None]
            is_wet = rng.random((n, len(lats), len(lons))) < rain_prob
            amount = rng.gamma(shape=1.2, scale=6.0, size=(n, len(lats), len(lons))) * bias_mult
            field = np.where(is_wet, amount, 0.0)
            units = "mm/day"
        return xr.DataArray(
            field, dims=("time", "lat", "lon"),
            coords={"time": time, "lat": lats, "lon": lons},
            attrs={"units": units},
        ).to_dataset(name=var_name)

    for var_name, kind, add_h, mult_h, add_s, mult_s in [
        ("tasmax", "+", 1.8, 1.0, 2.1, 1.0),
        ("pr", "*", 1.0, 1.0, 1.0, 1.15),
    ]:
        hist_path = root / f"{var_name}_hist.nc"
        sim_path = root / f"{var_name}_sim.nc"
        build(kind, add_h, mult_h).to_netcdf(hist_path)
        build(kind, add_s, mult_s).to_netcdf(sim_path)
        paths[f"{var_name}_hist"] = hist_path
        paths[f"{var_name}_sim"] = sim_path

    return paths


if __name__ == "__main__":
    out_root = Path(__file__).resolve().parent.parent / "data"
    result = generate_toy_dataset(out_root)
    print(f"Đã sinh dữ liệu toy tại: {out_root}")
    for key, p in sorted(result.items()):
        print(f" - {key}: {p}")
