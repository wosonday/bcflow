"""Các nhánh warn/fail của loaders.load_ref_points — không chỉ happy path.

Mỗi test tạo dữ liệu lỗi có chủ đích trên nền toy_paths (đã sinh sẵn), rồi
kiểm tra QACheck + hành vi (tiếp tục với phần còn lại, hay dừng cứng).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from bcflow.config_schema import RefPointConfig
from bcflow.loaders import load_ref_points
from bcflow.qa import QACheck


def test_wide_extra_station_in_metadata_warns_not_fails(toy_paths):
    """Metadata có 1 trạm thừa không có cột dữ liệu -> warn, bỏ qua, không raise."""
    meta = pd.read_csv(toy_paths["tasmax_meta"])
    meta_extra = pd.concat([meta, pd.DataFrame([{"id": "ST99", "name": "phantom", "lat": 15.0, "lon": 107.0}])])
    meta_extra_path = toy_paths["tasmax_meta"].parent / "meta_extra.csv"
    meta_extra.to_csv(meta_extra_path, index=False)

    qa: list[QACheck] = []
    cfg = RefPointConfig(station_layout="wide", metadata_path=str(meta_extra_path), path=str(toy_paths["tasmax_data"]))
    da = load_ref_points(cfg, qa)

    assert any(c.status == "warn" and "missing_columns" in c.name for c in qa)
    assert "phantom" not in da.station.values


def test_long_missing_one_station_file_warns_continues(toy_paths, tmp_path):
    """Thiếu file của 1 trạm (layout long) -> warn, 2 trạm còn lại vẫn nạp được."""
    import shutil

    folder_missing = tmp_path / "pr_ref_long_missing"
    shutil.copytree(toy_paths["pr_folder"], folder_missing)
    (folder_missing / "danang.csv").unlink()

    qa: list[QACheck] = []
    cfg = RefPointConfig(station_layout="long", metadata_path=str(toy_paths["pr_meta"]), path=str(folder_missing))
    da = load_ref_points(cfg, qa)

    assert any(c.status == "warn" and "missing_files" in c.name for c in qa)
    assert da.sizes["station"] == 2


def test_zero_matched_stations_raises_with_fail_check(toy_paths, tmp_path):
    """Dữ liệu hợp lệ nhưng không trạm nào khớp tên -> raise + QACheck 'fail'."""
    no_match = tmp_path / "no_match_wide.csv"
    pd.DataFrame({
        "time": pd.date_range("2000-01-01", periods=5),
        "ten_khong_ton_tai": [1, 2, 3, 4, 5],
    }).to_csv(no_match, index=False)

    qa: list[QACheck] = []
    cfg = RefPointConfig(station_layout="wide", metadata_path=str(toy_paths["tasmax_meta"]), path=str(no_match))

    with pytest.raises(ValueError, match="No station matched"):
        load_ref_points(cfg, qa)
    assert any(c.status == "fail" for c in qa)


def test_long_and_wide_layouts_both_warn_instead_of_hard_fail(toy_paths, tmp_path):
    """Cả 2 layout phải đối xứng: thiếu 1 phần dữ liệu -> warn (không raise cứng)."""
    import shutil

    # wide: thiếu 1 cột
    raw = pd.read_csv(toy_paths["tasmax_data"])
    raw_missing = raw.drop(columns=["danang"])
    wide_missing = tmp_path / "tasmax_wide_missing.csv"
    raw_missing.to_csv(wide_missing, index=False)

    qa_wide: list[QACheck] = []
    cfg_wide = RefPointConfig(station_layout="wide", metadata_path=str(toy_paths["tasmax_meta"]), path=str(wide_missing))
    da_wide = load_ref_points(cfg_wide, qa_wide)

    # long: thiếu 1 file
    folder_missing = tmp_path / "pr_long_missing2"
    shutil.copytree(toy_paths["pr_folder"], folder_missing)
    (folder_missing / "danang.csv").unlink()

    qa_long: list[QACheck] = []
    cfg_long = RefPointConfig(station_layout="long", metadata_path=str(toy_paths["pr_meta"]), path=str(folder_missing))
    da_long = load_ref_points(cfg_long, qa_long)

    assert da_wide.sizes["station"] == 2 and not any(c.status == "fail" for c in qa_wide)
    assert da_long.sizes["station"] == 2 and not any(c.status == "fail" for c in qa_long)
