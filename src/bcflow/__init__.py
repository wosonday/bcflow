"""Public API, loaded on demand so discovery needs only the standard library."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adjust import run_variable
    from .cli import main
    from .config_schema import OutputConfig, PipelineConfig, load_config
    from .pipeline import run_pipeline
    from .qa import QACheck, all_pass, failures, warnings_only
    from .variables import DEFAULTS, VARIABLES, VariableSpec
    from .writers import write_run

_EXPORT_MODULES = {
    "main": ".cli",
    "run_pipeline": ".pipeline",
    "run_variable": ".adjust",
    "write_run": ".writers",
    "load_config": ".config_schema",
    "OutputConfig": ".config_schema",
    "PipelineConfig": ".config_schema",
    "VariableSpec": ".variables",
    "VARIABLES": ".variables",
    "DEFAULTS": ".variables",
    "QACheck": ".qa",
    "all_pass": ".qa",
    "failures": ".qa",
    "warnings_only": ".qa",
}

__all__ = [
    "main",
    "run_pipeline",
    "run_variable",
    "write_run",
    "load_config",
    "OutputConfig",
    "PipelineConfig",
    "VariableSpec",
    "VARIABLES",
    "DEFAULTS",
    "QACheck",
    "all_pass",
    "failures",
    "warnings_only",
]


def __getattr__(name: str):
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
