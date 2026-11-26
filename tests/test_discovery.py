"""Discovery must work without loading any third-party runtime dependencies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_EXPECTED_METHODS = [
    {
        "name": "empirical_quantile_mapping",
        "class": "EmpiricalQuantileMapping",
        "summary": "Empirical Quantile Mapping bias-adjustment.",
        "family": "train_adjust",
        "min_vars": 1,
        "supports_projection": True,
        "native_adapt_freq": True,
        "uses_quantiles": True,
        "extra": None,
    },
    {
        "name": "detrended_quantile_mapping",
        "class": "DetrendedQuantileMapping",
        "summary": "Detrended Quantile Mapping bias-adjustment.",
        "family": "train_adjust",
        "min_vars": 1,
        "supports_projection": True,
        "native_adapt_freq": True,
        "uses_quantiles": True,
        "extra": None,
    },
    {
        "name": "quantile_delta_mapping",
        "class": "QuantileDeltaMapping",
        "summary": "Quantile Delta Mapping bias-adjustment.",
        "family": "train_adjust",
        "min_vars": 1,
        "supports_projection": True,
        "native_adapt_freq": True,
        "uses_quantiles": True,
        "extra": None,
    },
    {
        "name": "scaling",
        "class": "Scaling",
        "summary": "Scaling bias-adjustment.",
        "family": "train_adjust",
        "min_vars": 1,
        "supports_projection": True,
        "native_adapt_freq": False,
        "uses_quantiles": False,
        "extra": None,
    },
    {
        "name": "loci",
        "class": "LOCI",
        "summary": "Local Intensity Scaling (LOCI) bias-adjustment.",
        "family": "train_adjust",
        "min_vars": 1,
        "supports_projection": True,
        "native_adapt_freq": False,
        "uses_quantiles": False,
        "extra": None,
    },
    {
        "name": "extreme_values",
        "class": "ExtremeValues",
        "summary": "Adjustment correction for extreme values.",
        "family": "post",
        "min_vars": 1,
        "supports_projection": True,
        "native_adapt_freq": False,
        "uses_quantiles": True,
        "extra": None,
    },
    {
        "name": "variance_scaling",
        "class": "VarianceScaling",
        "summary": "Variance Scaling bias-adjustment.",
        "family": "train_adjust",
        "min_vars": 1,
        "supports_projection": True,
        "native_adapt_freq": False,
        "uses_quantiles": False,
        "extra": None,
    },
    {
        "name": "parametric_quantile_mapping",
        "class": "ParametricQuantileMapping",
        "summary": "Parametric Quantile Mapping bias-adjustment.",
        "family": "train_adjust",
        "min_vars": 1,
        "supports_projection": True,
        "native_adapt_freq": False,
        "uses_quantiles": True,
        "extra": None,
    },
    {
        "name": "power_transformation",
        "class": "PowerTransformation",
        "summary": "Power Transformation bias-adjustment.",
        "family": "train_adjust",
        "min_vars": 1,
        "supports_projection": True,
        "native_adapt_freq": False,
        "uses_quantiles": False,
        "extra": None,
    },
    {
        "name": "principal_components",
        "class": "PrincipalComponents",
        "summary": "Principal component adjustment.",
        "family": "train_adjust",
        "min_vars": 2,
        "supports_projection": True,
        "native_adapt_freq": False,
        "uses_quantiles": False,
        "extra": None,
    },
    {
        "name": "mbcn",
        "class": "MBCn",
        "summary": (
            "Multivariate bias correction function using the N-dimensional "
            "probability density function transform."
        ),
        "family": "train_adjust",
        "min_vars": 2,
        "supports_projection": True,
        "native_adapt_freq": False,
        "uses_quantiles": False,
        "extra": None,
    },
    {
        "name": "npdf_transform",
        "class": "NpdfTransform",
        "summary": "N-dimensional probability density function transform.",
        "family": "adjust_only",
        "min_vars": 2,
        "supports_projection": True,
        "native_adapt_freq": False,
        "uses_quantiles": False,
        "extra": None,
    },
    {
        "name": "otc",
        "class": "OTC",
        "summary": "Optimal Transport Correction.",
        "family": "adjust_only",
        "min_vars": 2,
        "supports_projection": False,
        "native_adapt_freq": True,
        "uses_quantiles": False,
        "extra": "POT",
    },
    {
        "name": "dotc",
        "class": "dOTC",
        "summary": "Dynamical Optimal Transport Correction.",
        "family": "adjust_only",
        "min_vars": 2,
        "supports_projection": True,
        "native_adapt_freq": True,
        "uses_quantiles": False,
        "extra": "POT",
    },
]

# Run in a fresh interpreter: the test runner/conftest already imports NumPy.
# AssertionError also prevents optional-dependency ImportError handlers from
# concealing an attempted third-party import.
_BLOCK_THIRD_PARTY = """
import sys

class StdlibOnly:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.partition('.')[0]
        if root != 'bcflow' and root not in sys.stdlib_module_names:
            raise AssertionError('Discovery attempted third-party import: ' + fullname)

sys.meta_path.insert(0, StdlibOnly())
"""


def _subprocess(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), *filter(None, [env.get("PYTHONPATH")])]
    )
    result = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    return result


@pytest.mark.parametrize("entrypoint", ["module", "console"])
@pytest.mark.parametrize("args", [("--version",), ("methods", "--json"), ("methods",)])
def test_discovery_is_stdlib_only(entrypoint, args):
    script = _BLOCK_THIRD_PARTY + textwrap.dedent(
        f"""
        import runpy
        sys.argv = ['bcflow', *sys.argv[1:]]
        try:
            if {entrypoint!r} == 'module':
                runpy.run_module('bcflow', run_name='__main__')
            else:
                from bcflow.cli import main
                raise SystemExit(main())
        except SystemExit as exc:
            assert exc.code == 0, exc.code
        assert 'bcflow.service' not in sys.modules
        assert 'bcflow.methods' not in sys.modules
        assert 'bcflow.pipeline' not in sys.modules
        """
    )
    result = _subprocess(script, *args)
    if args == ("--version",):
        from bcflow._version import bcflow_version

        assert result.stdout == f"bcflow {bcflow_version()}\n"
    elif "--json" in args:
        assert json.loads(result.stdout) == _EXPECTED_METHODS
        assert len(result.stdout.splitlines()) == 1
    else:
        assert result.stdout == "".join(
            f"{method['name']:<30} {method['summary']}\n"
            for method in _EXPECTED_METHODS
        )


def test_package_and_cli_imports_are_stdlib_only():
    _subprocess(
        _BLOCK_THIRD_PARTY
        + textwrap.dedent(
            """
            import bcflow
            import bcflow.cli
            assert callable(bcflow.main)
            assert bcflow.main is bcflow.cli.main
            assert 'run_pipeline' in dir(bcflow)
            assert 'bcflow.service' not in sys.modules
            assert 'bcflow.pipeline' not in sys.modules
            try:
                bcflow.not_a_public_export
            except AttributeError:
                pass
            else:
                raise AssertionError('Unknown exports must raise AttributeError')
            """
        )
    )


def test_discovery_metadata_matches_real_registry():
    from bcflow.discovery import METHOD_METADATA, get_method_meta
    from bcflow.methods import METHOD_REGISTRY
    from bcflow.service import list_methods

    methods = list_methods()
    assert methods == _EXPECTED_METHODS

    actual = [
        {
            "name": name,
            "class": cls.__name__,
            "summary": (cls.__doc__ or "").strip().splitlines()[0],
        }
        for name, cls in METHOD_REGISTRY.items()
    ]
    projected = [
        {"name": m["name"], "class": m["class"], "summary": m["summary"]}
        for m in methods
    ]
    assert projected == actual
    assert METHOD_METADATA == tuple(
        (m["name"], m["class"], m["summary"]) for m in _EXPECTED_METHODS
    )
    assert get_method_meta("quantile_delta_mapping").uses_quantiles is True
    with pytest.raises(ValueError, match="not registered"):
        get_method_meta("unknown_method")


def test_public_exports_are_preserved():
    from importlib import import_module

    import bcflow

    exports = {
        "main": "cli",
        "run_pipeline": "pipeline",
        "run_variable": "adjust",
        "write_run": "writers",
        "load_config": "config_schema",
        "OutputConfig": "config_schema",
        "PipelineConfig": "config_schema",
        "VariableSpec": "variables",
        "VARIABLES": "variables",
        "DEFAULTS": "variables",
        "QACheck": "qa",
        "all_pass": "qa",
        "failures": "qa",
        "warnings_only": "qa",
    }
    assert set(bcflow.__all__) == set(exports)
    for name, module in exports.items():
        value = getattr(bcflow, name)
        assert value is getattr(import_module(f"bcflow.{module}"), name)
        assert bcflow.__dict__[name] is value


def test_cli_service_remains_patchable(monkeypatch, capsys):
    import bcflow
    from bcflow import cli

    assert cli.service is bcflow.service
    monkeypatch.setattr(cli.service, "config_json_schema", lambda: {"patched": True})
    assert cli.main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out) == {"patched": True}
