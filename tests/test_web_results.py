"""Safe, bounded JSON/CSV readers for the writer's declared run layout."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from app.services import results


def _manifest(folder: Path, **updates) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    document = {
        "created": "2026-11-01T12:00:00+00:00",
        "status": "ok",
        "variables": {"tas": {"status": "ok", "evaluation": "tas/evaluation.csv"}},
    }
    document.update(updates)
    path = folder / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _evaluation(manifest: Path, content: str) -> Path:
    path = manifest.parent / "tas/evaluation.csv"
    path.parent.mkdir(exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _symlink(link: Path, target: Path):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")


def test_list_only_direct_and_immediate_run_manifests(tmp_path):
    direct = _manifest(tmp_path, created="2026-11-01")
    newest = _manifest(tmp_path / "run_new", created="2026-11-03", status="cancelled")
    older = _manifest(
        tmp_path / "run_old",
        created="2026-11-02",
        status="failed",
        variables={"tas": {"status": "failed"}, "pr": {"status": "skipped"}},
    )
    _manifest(tmp_path / "data", created="2099")
    _manifest(tmp_path / "run_old" / "run_nested", created="2099")
    corrupt = tmp_path / "run_corrupt"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("{partial", encoding="utf-8")
    _manifest(tmp_path / "run_invalid", variables=[])
    (tmp_path / "run_empty").mkdir()
    rows = results.list_manifests(str(tmp_path))
    assert [row["path"] for row in rows] == [str(newest), str(older), str(direct)]
    assert rows[1] == {
        "path": str(older),
        "created": "2026-11-02",
        "status": "failed",
        "variables": {"tas": "failed", "pr": "skipped"},
    }
    assert results.list_manifests(tmp_path, limit=1) == rows[:1]
    assert results.list_manifests(tmp_path / "run_old") == [
        {
            "path": str(tmp_path / "run_old/run_nested/manifest.json"),
            "created": "2099",
            "status": "ok",
            "variables": {"tas": "ok"},
        },
        rows[1],
    ]
    assert results.list_manifests(tmp_path, limit=0) == []
    assert results.list_manifests(tmp_path / "missing") == []


@pytest.mark.parametrize("limit", [-1, 1.5, True])
def test_invalid_listing_limits(tmp_path, limit):
    with pytest.raises(ValueError, match="limit"):
        results.list_manifests(tmp_path, limit=limit)


@pytest.mark.parametrize(
    "document",
    [
        [],
        {},
        {"variables": []},
        {"variables": {"tas": "ok"}},
        {"variables": {}, "status": []},
        {"variables": {}, "created": 123},
        {"variables": {"tas": {"status": False}}},
    ],
)
def test_invalid_manifest_mapping(tmp_path, document):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="Manifest"):
        results.load_manifest(path)


def test_manifest_is_unmodified_and_missing_status_is_unknown(tmp_path):
    path = _manifest(tmp_path, variables={"tas": {}}, extra={"nested": [1]})
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert results.load_manifest(str(path)) == expected
    loaded = results.load_manifest(path)
    loaded["extra"]["nested"].append(2)
    assert results.load_manifest(path) == expected
    assert results.list_manifests(tmp_path)[0]["variables"] == {"tas": "unknown"}


@pytest.mark.parametrize(
    "relative",
    [
        "../outside.json",
        "sub/../qa.json",
        "/etc/passwd",
        "C:\\qa.json",
        "C:qa.json",
        "\\\\server\\share\\qa.json",
        "..\\qa.json",
        "",
        "\x00",
    ],
)
def test_artifact_rejects_unsafe_paths(tmp_path, relative):
    manifest = _manifest(tmp_path / "run")
    with pytest.raises(ValueError, match="Artifact path"):
        results.artifact_path(manifest, relative)


def test_artifact_requires_file_and_blocks_symlink_escape(tmp_path):
    manifest = _manifest(tmp_path / "run")
    actual = manifest.parent / "qa.json"
    actual.write_text("[]", encoding="utf-8")
    assert results.artifact_path(str(manifest), "qa.json") == actual.resolve()
    with pytest.raises(ValueError, match="not a file"):
        results.artifact_path(manifest, "absent.json")
    with pytest.raises(ValueError, match="not a file"):
        results.artifact_path(manifest, ".")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "qa.json").write_text("[]", encoding="utf-8")
    _symlink(manifest.parent / "escape", outside)
    with pytest.raises(ValueError, match="escapes"):
        results.artifact_path(manifest, "escape/qa.json")
    _symlink(manifest.parent / "alias.json", actual)
    assert results.artifact_path(manifest, "alias.json") == actual.resolve()


def test_list_does_not_follow_symlinked_run_directories(tmp_path):
    root = tmp_path / "outputs"
    root.mkdir()
    outside = _manifest(tmp_path / "outside")
    _symlink(root / "run_link", outside.parent)
    _symlink(root / "manifest.json", outside)
    assert results.list_manifests(root) == []


def test_read_qa_only_uses_declaration_and_preserves_context(tmp_path):
    path = _manifest(tmp_path)
    checks = [
        {
            "name": "check",
            "status": "warn",
            "message": "test",
            "context": {"station": "001"},
        }
    ]
    qa = tmp_path / "qa.json"
    qa.write_text(json.dumps(checks), encoding="utf-8")
    assert results.read_qa(path) == []  # No guessing based on conventional filenames.
    _manifest(tmp_path, qa=None)
    assert results.read_qa(path) == []
    _manifest(tmp_path, qa="qa.json")
    before = copy.deepcopy(checks)
    assert results.read_qa(str(path)) == before
    for invalid in ({}, ["bad"], None):
        qa.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(ValueError, match="list of mappings"):
            results.read_qa(path)
    qa.unlink()
    with pytest.raises(ValueError, match="missing"):
        results.read_qa(path)
    _manifest(tmp_path, qa="../outside.json")
    with pytest.raises(ValueError, match="relative"):
        results.read_qa(path)


def test_json_artifacts_are_size_guarded_and_corrupt_files_are_skipped(tmp_path):
    path = _manifest(tmp_path / "run_large", qa="qa.json")
    qa = path.parent / "qa.json"
    with qa.open("wb") as stream:
        stream.seek(8 * 1024 * 1024)
        stream.write(b" ")
    with pytest.raises(ValueError, match="8 MiB"):
        results.read_qa(path)
    path.write_bytes(qa.read_bytes())
    with pytest.raises(ValueError, match="8 MiB"):
        results.load_manifest(path)
    assert results.list_manifests(tmp_path) == []
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="JSON artifact"):
        results.load_manifest(path)
    assert results.list_manifests(tmp_path) == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO test")
def test_listing_never_opens_non_regular_manifests(tmp_path):
    folder = tmp_path / "run_fifo"
    folder.mkdir()
    os.mkfifo(folder / "manifest.json")
    assert results.list_manifests(tmp_path) == []


def test_evaluation_caps_read_and_preserves_identifiers_and_types(
    tmp_path, monkeypatch
):
    path = _manifest(tmp_path)
    _evaluation(
        path,
        "period,station,metric,ideal,raw,corrected,improved\n"
        "calibration,001,mean_bias,0,2.5,1,True\n"
        "validation,NA,sd_ratio,1,,,\n"
        "validation,003,mean_bias,0,4,2,False\n",
    )
    original = results.pd.read_csv
    calls = []

    def bounded_read(*args, **kwargs):
        calls.append(kwargs["nrows"])
        return original(*args, **kwargs)

    monkeypatch.setattr(results.pd, "read_csv", bounded_read)
    table = results.read_evaluation(str(path), "tas", max_rows=2)
    assert calls == [3]
    assert len(table) == 2
    assert table.attrs["truncated"] is True
    assert table["station"].tolist() == ["001", "NA"]
    assert table["raw"].iloc[0] == 2.5
    assert pd.isna(table["raw"].iloc[1])
    assert pd.api.types.is_numeric_dtype(table["corrected"])
    assert str(table["improved"].dtype) == "boolean"
    assert bool(table["improved"].iloc[0])
    assert pd.isna(table["improved"].iloc[1])
    full = results.read_evaluation(path, "tas", max_rows=3)
    assert full.attrs["truncated"] is False
    assert len(full) == 3


def test_grid_evaluation_and_empty_header_are_supported(tmp_path):
    path = _manifest(tmp_path)
    artifact = _evaluation(
        path, "lat,lon,metric,raw,corrected\n10.5,106.5,mean_bias,NaN,0.5\n"
    )
    table = results.read_evaluation(path, "tas")
    assert table["lat"].iloc[0] == 10.5
    assert table["lon"].iloc[0] == 106.5
    assert pd.isna(table["raw"].iloc[0])
    artifact.write_text("station,metric,raw,corrected\n", encoding="utf-8")
    assert results.read_evaluation(path, "tas").empty


@pytest.mark.parametrize("max_rows", [0, -1, True, 1.5])
def test_invalid_row_limits(tmp_path, max_rows):
    with pytest.raises(ValueError, match="max_rows"):
        results.read_evaluation(tmp_path / "manifest.json", "tas", max_rows=max_rows)


@pytest.mark.parametrize(
    "csv, error",
    [
        ("station,raw,corrected\na,1,2\n", "metric"),
        ("metric,raw,corrected\nmean_bias,1,2\n", "station"),
        ("lat,metric,raw,corrected\n1,mean_bias,1,2\n", "lon"),
        ("station,metric,raw,corrected\na,mean_bias,bad,2\n", "raw.*numeric"),
        ("station,metric,raw,corrected,improved\na,mean_bias,1,2,maybe\n", "improved"),
        ("", "Cannot read evaluation CSV"),
    ],
)
def test_evaluation_validates_columns_and_types(tmp_path, csv, error):
    path = _manifest(tmp_path)
    _evaluation(path, csv)
    with pytest.raises(ValueError, match=error):
        results.read_evaluation(path, "tas")


def test_evaluation_requires_declared_csv_and_safe_path(tmp_path):
    path = _manifest(tmp_path, variables={"tas": {"status": "failed"}})
    _evaluation(path, "station,metric,raw,corrected\na,mean_bias,1,0\n")
    with pytest.raises(ValueError, match="not declared"):
        results.read_evaluation(path, "unknown")
    with pytest.raises(ValueError, match="No evaluation"):
        results.read_evaluation(path, "tas")
    outside = tmp_path.parent / f"{tmp_path.name}_outside.csv"
    outside.write_text(
        "station,metric,raw,corrected\na,mean_bias,1,0\n", encoding="utf-8"
    )
    try:
        _manifest(tmp_path, variables={"tas": {"evaluation": str(outside)}})
        with pytest.raises(ValueError, match="relative"):
            results.read_evaluation(path, "tas")
    finally:
        outside.unlink()
    nc = tmp_path / "scen.nc"
    nc.write_bytes(b"not a dataset")
    _manifest(tmp_path, variables={"tas": {"evaluation": "scen.nc"}})
    with pytest.raises(ValueError, match="CSV"):
        results.read_evaluation(path, "tas")


def test_results_import_does_not_load_pipeline_netcdf_or_ui():
    script = """
import sys
class BlockBackend:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'streamlit', 'xarray', 'xsdba', 'netCDF4', 'scipy'}:
            raise AssertionError('Unexpected import: ' + fullname)
sys.meta_path.insert(0, BlockBackend())
from app.services.results import list_manifests
assert 'bcflow.service' not in sys.modules
assert 'bcflow.writers' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_results_data_helpers(tmp_path):
    history = [
        {"kind": "validate", "active": False, "summary": {"manifest": "skip.json"}},
        {"kind": "run", "active": True, "summary": {"manifest": "active.json"}},
        {"kind": "run", "active": False, "summary": {"manifest": "out/manifest.json"}},
    ]
    assert results.latest_manifest(history, tmp_path) == str(
        (tmp_path / "out/manifest.json").absolute()
    )
    assert results.latest_manifest([], tmp_path) is None

    frame = results.qa_checks_frame(
        [
            {"name": "c1", "status": "pass", "message": "ok", "context": {"variable": "tas"}},
            {"name": "c2", "status": "warn", "message": "w"},
        ]
    )
    assert list(frame.columns) == ["Status", "Variable", "Check", "Message", "Context"]
    assert frame["Variable"].tolist() == ["tas", "Run"]

    manifest = {
        "qa": "qa.json",
        "config": None,
        "variables": {
            "tas": {
                "evaluation": "tas/eval.csv",
                "scen": {"netcdf": "tas/scen.nc", "zarr": None},
            }
        },
    }
    assert results.declared_artifacts(manifest) == [
        ("qa", "qa.json"),
        ("tas / evaluation", "tas/eval.csv"),
        ("tas / scen / netcdf", "tas/scen.nc"),
    ]

    df = pd.DataFrame(
        {
            "station": ["001", "001", "002"],
            "raw": [float("inf"), 1.0, 2.0],
            "corrected": [0.5, 0.6, 0.7],
            "units": ["degC", "degC", "degC"],
        }
    )
    cleaned = results.finite_values(df)
    assert pd.isna(cleaned["raw"].iloc[0])
    assert pd.api.types.is_float_dtype(cleaned["raw"])
    chart = results.chart_data(cleaned)
    assert chart["Location"].tolist() == ["001", "002"]
    assert results.metric_label("mean_bias", {}, cleaned) == "mean bias (degC)"
    assert (
        results.metric_label("sd_ratio", {"units": "degC"}, cleaned)
        == "sd ratio (dimensionless ratio)"
    )
