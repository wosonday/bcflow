"""Document editing is independent of pipeline/schema validation and the UI."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.services import config as service
from bcflow.config_loader import load_config_file


@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".json"])
def test_load_and_resolve_match_backend(tmp_path, suffix):
    raw = {
        "model": {"hist": "../model/{var}_hist.nc", "sim": "../model/{var}_sim.nc"},
        "stations": {"metadata": "stations.csv", "data_dir": "weather"},
        "nested": [{"path": "grid.nc"}, {"metadata": 123}],
        "output": {"path": "outputs"},
        "file_pattern": "{id}.csv",
    }
    original = copy.deepcopy(raw)
    folder = tmp_path / "configs"
    folder.mkdir()
    path = folder / f"config{suffix}"
    text = json.dumps(raw) if suffix == ".json" else yaml.safe_dump(raw)
    # Raw digest must include CRLF; document text must not translate it.
    content = text.replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(content)
    document = service.load_document(str(path))
    assert document == {
        "path": str(path.resolve()),
        "text": content.decode("utf-8"),
        "format": suffix,
        "base_dir": str(folder.resolve()),
        "digest": hashlib.sha256(content).hexdigest(),
    }
    resolved = service.resolve_document(document["text"], suffix, str(folder))
    assert resolved == load_config_file(path)
    assert resolved["model"]["hist"] == str(
        (tmp_path / "model/{var}_hist.nc").resolve()
    )
    assert resolved["file_pattern"] == "{id}.csv"
    assert raw == original


@pytest.mark.parametrize(
    "text, suffix, error",
    [
        ("{broken", ".json", "JSON"),
        ("key: [broken", ".yaml", "YAML"),
        ("!!python/object/apply:os.system ['echo unsafe']", ".yml", "YAML"),
        ("[]", ".json", "mapping"),
        ("null", ".json", "mapping"),
        ("true", ".yaml", "mapping"),
        ("", ".yaml", "mapping"),
        ("42", ".yml", "mapping"),
    ],
)
def test_invalid_documents_raise_value_error(tmp_path, text, suffix, error):
    with pytest.raises(ValueError, match=error):
        service.resolve_document(text, suffix, tmp_path)
    path = tmp_path / f"invalid{suffix}"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        service.load_document(path)


@pytest.mark.parametrize("suffix", ["yaml", ".toml", "", ".txt"])
def test_formats_are_whitelisted(tmp_path, suffix):
    with pytest.raises(ValueError, match="Unsupported config format"):
        service.resolve_document("{}", suffix, tmp_path)
    with pytest.raises(ValueError, match="Unsupported config format"):
        service.save_copy({}, tmp_path / f"copy{suffix}")


def test_mapping_parse_is_not_backend_validation(tmp_path):
    assert service.resolve_document("not_a_pipeline: true", ".yaml", tmp_path) == {
        "not_a_pipeline": True
    }


def test_load_bounds_size_and_decodes_utf8(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        service.load_document(path)
    path.write_bytes(b"{}" + b" " * (2 * 1024 * 1024 - 2))
    assert service.load_document(path)["text"].startswith("{}")
    with path.open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="2 MiB"):
        service.load_document(path)
    with pytest.raises(ValueError, match="not found"):
        service.load_document(tmp_path / "missing.yaml")


def test_digest_is_canonical_and_distinct_from_raw_digest(tmp_path):
    first = {"z": [1, {"b": 2, "a": 1}], "a": "café"}
    second = {"a": "café", "z": [1, {"a": 1, "b": 2}]}
    canonical = json.dumps(
        first, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert service.config_digest(first) == service.config_digest(second) == expected
    assert service.config_digest({"a": 1}) != service.config_digest({"a": 2})
    path = tmp_path / "source.json"
    path.write_text(json.dumps(first, indent=2), encoding="utf-8")
    assert service.load_document(path)["digest"] != expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), object()])
def test_non_json_values_cannot_be_hashed_or_saved(tmp_path, value):
    with pytest.raises(ValueError, match="finite JSON"):
        service.config_digest({"value": value})
    with pytest.raises(ValueError, match="finite JSON"):
        service.save_copy({"value": value}, tmp_path / "copy.yaml")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("suffix", [".json", ".yaml", ".yml"])
def test_save_copy_preserves_resolved_paths_and_source(tmp_path, suffix):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "config.yaml"
    text = "model:\n  hist: data/{var}.nc\noutput:\n  path: results\n"
    source.write_text(text, encoding="utf-8")
    config = service.resolve_document(text, ".yaml", source_dir)
    before = copy.deepcopy(config)
    target_dir = tmp_path / "elsewhere"
    target_dir.mkdir()
    target = target_dir / f"copy{suffix}"
    saved = service.save_copy(config, str(target))
    assert saved == target.absolute()
    assert load_config_file(saved) == config
    assert config == before
    assert source.read_text(encoding="utf-8") == text
    assert list(target_dir.iterdir()) == [target]
    with pytest.raises(FileExistsError):
        service.save_copy({"different": True}, target)
    assert load_config_file(saved) == config
    service.save_copy({"different": True}, target, overwrite=True)
    assert load_config_file(saved) == {"different": True}
    assert list(target_dir.iterdir()) == [target]


def test_failed_atomic_publication_cleans_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "copy.json"
    target.write_text("original", encoding="utf-8")

    def fail_replace(source, destination):
        assert Path(source).read_text(encoding="utf-8").startswith("{")
        assert destination.read_text(encoding="utf-8") == "original"
        raise OSError("publication failed")

    monkeypatch.setattr(service.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failed"):
        service.save_copy({}, target, overwrite=True)
    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [target]


def test_recursive_or_excessively_nested_documents_raise_value_error(tmp_path):
    with pytest.raises(ValueError, match="YAML"):
        service.resolve_document("key: " + "[" * 2000 + "]" * 2000, ".yaml", tmp_path)
    with pytest.raises(ValueError):
        service.resolve_document(
            '{"key":' + "[" * 2000 + "]" * 2000 + "}", ".json", tmp_path
        )
    with pytest.raises(ValueError, match="resolve config paths"):
        service.resolve_document("root: &root {child: *root}", ".yaml", tmp_path)


def test_config_import_does_not_load_scientific_backend_or_ui():
    script = """
import sys
class BlockBackend:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'streamlit', 'numpy', 'pandas', 'xarray', 'xsdba', 'netCDF4', 'scipy'}:
            raise AssertionError('Unexpected import: ' + fullname)
sys.meta_path.insert(0, BlockBackend())
from app.services.config import resolve_document
assert resolve_document('{}', '.json', '.') == {}
assert 'bcflow.config_schema' not in sys.modules
assert 'bcflow.service' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
