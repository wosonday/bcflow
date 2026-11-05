"""Xây dựng Grouper của xsdba từ config, và tự nhận diện tên chiều lat/lon.

Mẫu alias dict (thay vì dò danh sách ứng viên) port từ
`swatprep/core/weather.py::station_column()` (repo wosonday/swat-preprocessing):
mọi tên cột/chiều được chuẩn hoá về MỘT tên chính tắc qua bảng ánh xạ cố định,
tránh tình huống "mơ hồ" giả khi dataset có cả tên gốc lẫn alias của nó
(vd. có cả 'lat' và 'latitude' cùng lúc không phải là xung đột).
"""

from __future__ import annotations

import xarray as xr
from xsdba.base import Grouper

from .qa import QACheck

# Alias cho tên chiều không gian — mở rộng danh sách này khi gặp quy ước mới,
# không thêm nhánh if/elif.
_LATLON_ALIASES = {
    "latitude": "lat",
    "nav_lat": "lat",
    "y_latitude": "lat",
    "y": "lat",
    "longitude": "lon",
    "nav_lon": "lon",
    "x_longitude": "lon",
    "x": "lon",
}


def _canonical(name: str) -> str:
    key = name.strip().lower()
    return _LATLON_ALIASES.get(key, key)


def build_grouper(var_cfg) -> str | Grouper:
    """Build xsdba.base.Grouper from VariableConfig.group (validated by pydantic)."""
    g = var_cfg.group
    name = f"{g.dim}.{g.prop}" if g.prop else g.dim
    return Grouper(name, window=g.window, add_dims=g.add_dims)


def resolve_latlon_dims(
    da: xr.DataArray, grid_cfg, qa: list[QACheck], *, source_label: str = ""
) -> tuple[str, str]:
    """Return (lat_dim, lon_dim) exist in da.

    Absolute priority: lat_dim/lon_dim declare explicitly in config.
    If not declared: chuẩn hoá mọi dim/coord hiện có qua alias dict,
    tìm tên chính tắc 'lat'/'lon'. Không khai báo + không dò được -> fail rõ ràng.
    """
    tag = f" ({source_label})" if source_label else ""
    available = set(da.coords) | set(da.dims)
    canon_to_actual = {_canonical(n): n for n in available}

    lat_dim = grid_cfg.lat_dim
    lon_dim = grid_cfg.lon_dim
    auto_used = []

    if lat_dim is None:
        lat_dim = canon_to_actual.get("lat")
        if lat_dim:
            auto_used.append(f"lat_dim='{lat_dim}'")
    if lon_dim is None:
        lon_dim = canon_to_actual.get("lon")
        if lon_dim:
            auto_used.append(f"lon_dim='{lon_dim}'")

    if lat_dim is None or lon_dim is None:
        qa.append(QACheck(
            name=f"grouping:latlon_not_found{tag}",
            status="fail",
            message=(
                f"Could not detect lat/lon{tag}. "
                f"Found: {sorted(available)}. "
                "Accepted: lat/latitude/nav_lat/y_latitude and lon/longitude/nav_lon/x_longitude. "
                "Please set lat_dim/lon_dim in the config."
            ),
            context={"available": sorted(available)},
        ))
        raise ValueError(f"Could not detect lat/lon{tag} -- see QACheck 'grouping:latlon_not_found{tag}'.")

    if auto_used:
        qa.append(QACheck(
            name=f"grouping:latlon_auto_detected{tag}",
            status="pass",
            message=f"Tự động nhận diện chiều không gian{tag}: {', '.join(auto_used)}.",
        ))

    return lat_dim, lon_dim
