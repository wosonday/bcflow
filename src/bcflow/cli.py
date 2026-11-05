"""
CLI: `bcflow <config.yaml>` — run pipeline, write results, print QA report.

Exit codes:
    0  every variable computed and written
    1  the run finished, but at least one QACheck failed
    2  whole-run error: config file unreadable/invalid YAML, invalid 'output'
       section, or the output folder cannot be created
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config_loader import load_config_file
from .config_schema import OutputConfig
from .pipeline import run_pipeline
from .writers import write_run

_ICON = {"pass": "[PASS]", "warn": "[WARN]", "fail": "[FAIL]"}


def _print_report(out: dict) -> int:
    print("=" * 70)
    print("QA REPORT")
    print("=" * 70)
    for check in out["qa"]:
        print(f"{_ICON[check.status]} {check.name}: {check.message}")

    print()
    print("=" * 70)
    print("RESULTS PER VARIABLE")
    print("=" * 70)
    for name, res in out["results"].items():
        status = "OK" if res is not None else "FAILED (see QA report)"
        print(f"{name}: {status}")

    n_fail = sum(1 for c in out["qa"] if c.status == "fail")
    n_warn = sum(1 for c in out["qa"] if c.status == "warn")
    n_ok = sum(1 for v in out["results"].values() if v is not None)
    print(f"\nTotal: {n_fail} fail, {n_warn} warn, {n_ok}/{len(out['results'])} variables succeeded.")
    return 0 if n_fail == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bcflow", description="Run bias-correction pipeline from config.yaml")
    parser.add_argument("config", type=Path, help="Path to config.yaml file")
    args = parser.parse_args(argv)

    try:
        raw_config = load_config_file(args.config)
        # Validated here, not per variable inside run_pipeline(): a bad 'output'
        # section is a whole-run error (no single variable is at fault), so it
        # stops the run before any variable is computed. pydantic's
        # ValidationError subclasses ValueError, so this clause catches it too.
        output_cfg = OutputConfig.model_validate(raw_config.get("output") or {})
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    out = run_pipeline(raw_config)

    try:
        manifest_path = write_run(out, output_cfg)  # before the report, so write failures appear in it
    except OSError as exc:  # e.g. output folder not writable
        _print_report(out)
        print(f"[FAIL] Cannot create the output folder: {exc}", file=sys.stderr)
        return 2

    code = _print_report(out)
    print(f"Outputs written to: {manifest_path.parent}")
    return code


if __name__ == "__main__":
    sys.exit(main())
