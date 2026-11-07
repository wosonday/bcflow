"""Generate a small synthetic dataset in the swat-preprocessing layout.

    <root>/weather_stations.csv        id,name,lat,lon,elev (3 stations)
    <root>/weather/<id>.csv            date,precip,tmax (21 years, daily)
    <root>/model_hist_<var>.nc         model hist, one file per variable (pr, tasmax)
    <root>/model_sim_<var>.nc          model sim, with a deliberate extra bias
    <root>/obs_grid.nc                 gridded observations (0.5 deg, pr + tasmax,
                                       some sea cells empty, units "var_unit" as
                                       CDO writes them) for the gridded reference

The model carries known biases (tasmax +1.8 degC in hist, +2.1 in sim;
more and lighter rain days than the stations), so the tests can check that
the correction reduces them.

generate_toy_dataset(root) is called from the CLI (writes to data/toy/) and
from the pytest fixtures (writes to tmp_path) -- one logic, written once.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

STATIONS = pd.DataFrame(
    {
        "id": ["ST01", "ST02", "ST03"],
        "name": ["hanoi", "danang", "hcmc"],
        "lat": [21.03, 16.05, 10.78],
        "lon": [105.85, 108.21, 106.70],
        "elev": [16, 5, 19],
    }
)


def _wet_season(doy: np.ndarray) -> np.ndarray:
    return np.clip(np.sin(2 * np.pi * (doy - 150) / 365), 0, None)


def generate_toy_dataset(
    root: Path, seed: int = 42, n_years: int = 21
) -> dict[str, Path]:
    """Write every toy file under `root`; return the paths, keyed by role."""
    root = Path(root)
    weather = root / "weather"
    weather.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    time = pd.date_range("2000-01-01", periods=365 * n_years, freq="D")
    n = len(time)
    doy = time.day_of_year.to_numpy()

    # --- stations: metadata + one CSV per station ---------------------------
    meta = root / "weather_stations.csv"
    STATIONS.to_csv(meta, index=False)
    for i, sid in enumerate(STATIONS["id"]):
        tmax = (
            -10 * np.cos(2 * np.pi * doy / 365)
            + 27
            - 2 * i
            + rng.normal(0, 1.2, size=n)
        )
        is_wet = rng.random(n) < 0.15 + 0.5 * _wet_season(doy)
        precip = np.where(is_wet, rng.gamma(shape=1.5, scale=8.0, size=n), 0.0)
        pd.DataFrame(
            {
                "date": time.strftime("%Y-%m-%d"),
                "precip": precip.round(2),
                "tmax": tmax.round(2),
            }
        ).to_csv(weather / f"{sid}.csv", index=False)

    # --- model: regular 6 x 6 grid over Vietnam, deliberately biased ---------
    lats = np.linspace(9.0, 22.5, 6)
    lons = np.linspace(104.5, 109.5, 6)
    shape = (n, lats.size, lons.size)

    def field(var: str, bias_add: float, bias_mult: float) -> xr.Dataset:
        if var == "tasmax":
            seasonal = (-10 * np.cos(2 * np.pi * doy / 365) + 27)[:, None, None]
            values, units = seasonal + rng.normal(0, 1.2, size=shape) + bias_add, "degC"
        else:
            rain_prob = (0.25 + 0.5 * _wet_season(doy))[:, None, None]
            is_wet = rng.random(shape) < rain_prob
            amount = rng.gamma(shape=1.2, scale=6.0, size=shape) * bias_mult
            values, units = np.where(is_wet, amount, 0.0), "mm/day"
        return xr.DataArray(
            values,
            dims=("time", "lat", "lon"),
            coords={"time": time, "lat": lats, "lon": lons},
            attrs={"units": units},
        ).to_dataset(name=var)

    paths: dict[str, Path] = {
        "stations_meta": meta,
        "weather_dir": weather,
        "hist_template": root / "model_hist_{var}.nc",
        "sim_template": root / "model_sim_{var}.nc",
    }
    for var, add_h, mult_h, add_s, mult_s in [
        ("tasmax", 1.8, 1.0, 2.1, 1.0),
        ("pr", 0.0, 1.0, 0.0, 1.15),
    ]:
        for which, add, mult in (("hist", add_h, mult_h), ("sim", add_s, mult_s)):
            path = root / f"model_{which}_{var}.nc"
            field(var, add, mult).to_netcdf(path)
            paths[f"{which}_{var}"] = path

    paths["obs_grid"] = _obs_grid(root / "obs_grid.nc", time, doy, seed + 1)
    return paths


def _obs_grid(path: Path, time: pd.DatetimeIndex, doy: np.ndarray, seed: int) -> Path:
    """0.5-degree observations, stamped at 12:00 like many gridded products.

    Its own random generator, so adding it changed none of the files above.
    """
    rng = np.random.default_rng(seed)
    lats = np.arange(9.25, 22.5, 0.5)
    lons = np.arange(104.75, 109.5, 0.5)
    shape = (time.size, lats.size, lons.size)
    lat_trend = (lats - lats.mean())[None, :, None]
    tasmax = (
        (-10 * np.cos(2 * np.pi * doy / 365) + 27)[:, None, None]
        - 0.4 * lat_trend
        + rng.normal(0, 1.2, size=shape)
    )
    is_wet = rng.random(shape) < (0.15 + 0.5 * _wet_season(doy))[:, None, None]
    pr = np.where(is_wet, rng.gamma(shape=1.5, scale=8.0, size=shape), 0.0)
    sea = np.zeros((lats.size, lons.size), dtype=bool)
    sea[:4, -3:] = True  # a corner of open sea: no data at all
    tasmax[:, sea], pr[:, sea] = np.nan, np.nan
    coords = {"time": time + pd.Timedelta(hours=12), "lat": lats, "lon": lons}
    ds = xr.Dataset(
        {
            "pr": (("time", "lat", "lon"), pr.astype("float32"), {"units": "var_unit"}),
            "tasmax": (
                ("time", "lat", "lon"),
                tasmax.astype("float32"),
                {"units": "var_unit"},
            ),
        },
        coords=coords,
    )
    ds.to_netcdf(path)
    return path


if __name__ == "__main__":
    out_root = Path(__file__).resolve().parent.parent / "data" / "toy"
    result = generate_toy_dataset(out_root)
    print(f"Toy data written to: {out_root}")
    for key, p in sorted(result.items()):
        print(f" - {key}: {p}")
