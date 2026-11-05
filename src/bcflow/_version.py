"""Installed bcflow version, kept in its own module so writers.py and
service.py can both use it without importing each other."""

from importlib.metadata import PackageNotFoundError, version


def bcflow_version() -> str:
    try:
        return version("bcflow")
    except PackageNotFoundError:  # running from a source tree that is not installed
        return "unknown"
