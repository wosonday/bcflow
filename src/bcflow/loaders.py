"""Đọc dữ liệu thô (CSV điểm trạm, NetCDF/Zarr raster) thành xr.DataArray chuẩn hoá.

Quy ước lỗi theo mẫu của `swatprep/converters/station_weather.py`:
- Thiếu 1 phần dữ liệu đã khai báo trong metadata -> QACheck 'warn', bỏ qua
  phần đó, tiếp tục với phần còn lại.
- Không còn gì để xử lý (0 trạm khớp) -> QACheck 'fail' rồi raise, vì tiếp
  tục với DataArray rỗng sẽ cho lỗi khó hiểu hơn ở bước xsdba.train().
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import xarray as xr

from .qa import QACheck


def load_ref_points(ref_cfg, qa: list[QACheck], var_units: str = "") -> xr.DataArray:
    """ref_cfg: RefPointConfig (validated by pydantic)."""
    meta = pd.read_csv(ref_cfg.metadata_path).set_index(ref_cfg.match_key)

    if ref_cfg.station_layout == "long":
        folder = Path(ref_cfg.path)
        series: dict[str, pd.Series] = {}
        missing: list[str] = []
        for key in meta.index:
            f = folder / f"{key}.csv"
            if not f.exists():
                missing.append(key)
                continue
            df = pd.read_csv(f, parse_dates=["time"])
            series[key] = df.set_index("time")["value"]
        if missing:
            qa.append(QACheck(
                name="ref_points:long:missing_files",
                status="warn",
                message=f"{len(missing)} trạm có trong metadata nhưng không có file dữ liệu, sẽ bị bỏ qua.",
                context={"missing_stations": sorted(missing)},
            ))
        wide = pd.DataFrame(series)

    elif ref_cfg.station_layout == "wide":
        raw = pd.read_csv(ref_cfg.path, parse_dates=["time"]).set_index("time")
        matched = [c for c in meta.index if c in raw.columns]
        missing = sorted(set(meta.index) - set(raw.columns))
        if missing:
            qa.append(QACheck(
                name="ref_points:wide:missing_columns",
                status="warn",
                message=f"{len(missing)} trạm có trong metadata nhưng không có cột dữ liệu, sẽ bị bỏ qua.",
                context={"missing_stations": missing},
            ))
        wide = raw[matched]

    else:  # không thể xảy ra nếu đi qua pydantic, giữ lại cho an toàn khi gọi trực tiếp
        raise ValueError(f"Invalid station_layout: {ref_cfg.station_layout!r}. Valid layouts: ['long', 'wide'].")

    if wide.empty:
        qa.append(QACheck(
            name="ref_points:no_match",
            status="fail",
            message="No station matched between metadata and data -- check match_key.",
        ))
        raise ValueError("No station matched -- see QACheck 'ref_points:no_match'.")

    qa.append(QACheck(
        name="ref_points:loaded",
        status="pass",
        message=f"Đã nạp {len(wide.columns)} trạm (layout={ref_cfg.station_layout}).",
        context={"n_stations": len(wide.columns), "stations": sorted(wide.columns)},
    ))

    stations = wide.columns
    return xr.DataArray(
        wide.values,
        dims=("time", "station"),
        coords={
            "time": wide.index,
            "station": stations,
            "lat": ("station", meta.loc[stations, "lat"].values),
            "lon": ("station", meta.loc[stations, "lon"].values),
        },
        attrs={"units": var_units},
    )


def _open_raw(raster_cfg) -> xr.Dataset | xr.DataArray:
    if raster_cfg.format == "zarr":
        return xr.open_zarr(raster_cfg.path)
    if raster_cfg.format == "netcdf":
        return xr.open_dataset(raster_cfg.path)
    raise ValueError(f"Unsupported raster format: {raster_cfg.format!r}. Valid formats: ['netcdf', 'zarr'].")


def load_raster_variable(raster_cfg, var_name: str) -> xr.DataArray:
    """Mở 1 nguồn raster (NetCDF/Zarr) và trả về đúng 1 biến dưới dạng DataArray."""
    src = _open_raw(raster_cfg)
    return src[var_name] if isinstance(src, xr.Dataset) else src

def load_variable_sources(var_cfg, qa: list[QACheck]):
    """Load every raw input one variable needs for bias correction.

    Returns (ref, hist_raw, sim_raw, grid_cfg):
    - ref: station-point DataArray (time, station) -- already matched to metadata.
    - hist_raw, sim_raw: RAW raster DataArrays, NOT yet extracted to ref's
      points. Extraction is extract.extract_nearest()'s job, since it needs
      the raster AND ref together.
    - grid_cfg: the RasterSourceConfig describing hist_raw/sim_raw's grid
      (grid_type, lat_dim/lon_dim), needed by extract_nearest().

    Supports both ways model data can be declared for a variable (mutually
    exclusive -- enforced by VariableDataConfig's pydantic validator):
    split ('hist' + 'sim' sources) or combined ('hist_sim', sliced by period).
    """

    name = var_cfg.name
    ref = load_ref_points(var_cfg.data.ref, qa, var_units=var_cfg.units)

    d = var_cfg.data
    if d.hist_sim is not None:
        src_cfg = d.hist_sim
        src = load_raster_variable(src_cfg, name)
        h0, h1 = src_cfg.hist_period
        s0, s1 = src_cfg.sim_period
        hist_raw = src.sel(time=slice(h0, h1))
        sim_raw = src.sel(time=slice(s0, s1))
        grid_cfg = src_cfg
    else:
        hist_raw = load_raster_variable(d.hist, name)
        sim_raw = load_raster_variable(d.sim, name)
        grid_cfg = d.hist

    return ref, hist_raw, sim_raw, grid_cfg