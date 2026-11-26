"""Public result readers work without the web application."""

from __future__ import annotations

import json
import subprocess
import sys

import pandas as pd
import pytest

from bcflow import results


def test_results_import_is_lightweight():
    script = """
import sys
class BlockBackend:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'app', 'streamlit', 'pandas', 'xarray', 'xsdba'}:
            raise AssertionError('Unexpected import: ' + fullname)
sys.meta_path.insert(0, BlockBackend())
import bcflow.results
assert 'bcflow.service' not in sys.modules
assert 'bcflow.writers' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_config_path_resolution_is_public_and_non_mutating(tmp_path):
    config = {"model": {"hist": "../data/{var}.nc"}, "other": [{"path": "x.nc"}]}
    resolved = results.resolve_config_paths(config, tmp_path / "configs")
    assert resolved == {
        "model": {"hist": str((tmp_path / "data/{var}.nc").resolve())},
        "other": [{"path": str((tmp_path / "configs/x.nc").resolve())}],
    }
    assert config["model"]["hist"] == "../data/{var}.nc"


def test_manifest_and_artifacts_are_readable_without_app(tmp_path):
    run = tmp_path / "run_one"
    run.mkdir()
    manifest = run / "manifest.json"
    document = {
        "created": "2026-11-01",
        "status": "ok",
        "qa": "qa.json",
        "variables": {"tas": {"status": "ok", "evaluation": "tas/evaluation.csv"}},
    }
    manifest.write_text(json.dumps(document), encoding="utf-8")
    (run / "qa.json").write_text("[]", encoding="utf-8")
    (run / "tas").mkdir()
    (run / "tas/evaluation.csv").write_text(
        "station,metric,raw,corrected\n001,mean_bias,inf,0.5\n",
        encoding="utf-8",
    )
    assert results.load_manifest(manifest) == document
    assert results.list_manifests(tmp_path)[0]["variables"] == {"tas": "ok"}
    assert results.artifact_path(manifest, "qa.json") == (run / "qa.json").resolve()
    assert results.read_qa(manifest) == []
    assert results.declared_artifacts(document) == [
        ("qa", "qa.json"), ("tas / evaluation", "tas/evaluation.csv")
    ]
    data = results.read_evaluation(manifest, "tas")
    assert data["station"].tolist() == ["001"]
    assert pd.isna(results.finite_values(data)["raw"].iloc[0])
    assert data["raw"].iloc[0] == float("inf")
    with pytest.raises(ValueError, match="relative"):
        results.artifact_path(manifest, "../qa.json")
