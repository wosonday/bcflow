"""
CLI: `bcflow <config.yaml>` — run pipeline, print QA report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config_loader import load_config_file
from .pipeline import run_pipeline

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
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2  # error of file config (cannot read/cannot be parsed as YAML) -- different error code from "pipeline ran but has failures"

    out = run_pipeline(raw_config)
    return _print_report(out)


if __name__ == "__main__":
    sys.exit(main())