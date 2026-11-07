"""Command-line interface for bcflow.

    bcflow run CONFIG [--json]        run the pipeline and write the outputs
    bcflow validate CONFIG [--json]   check a config without running it
    bcflow inspect PATH [--json]      describe a data file: variables, units, time range
    bcflow methods [--json]           list the available correction methods
    bcflow schema                     print the JSON Schema of config.yaml

--json is the machine interface used by the GPUI (Rust) desktop app, which
runs bcflow as a subprocess. In that mode stdout carries ONLY JSON (ASCII,
so the Windows console encoding never matters): `run` prints one JSON
object per line -- progress events, the last one {"event": "run_finished", ...}
-- and every other command prints a single JSON document. Library warnings
and tracebacks go to stderr.

Exit codes:
    0  success (run: no failed check; validate: config valid, warnings allowed)
    1  finished with failures (run: a variable failed; validate: config invalid)
    2  could not start (unreadable config, bad 'output' section, path not found)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from . import service

_ICON = {"pass": "[PASS]", "warn": "[WARN]", "fail": "[FAIL]"}


def _json_default(obj):
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)  # Path, timestamps, ...


def _emit_json(obj) -> None:
    print(json.dumps(obj, default=_json_default), flush=True)


def _print_pretty(obj) -> None:
    print(json.dumps(obj, indent=2, default=_json_default, ensure_ascii=False))


# --- commands ---------------------------------------------------------------


def _print_run_report(summary: dict) -> None:
    if summary["qa"]:
        print("=" * 70)
        print("QA REPORT")
        print("=" * 70)
        for check in summary["qa"]:
            print(f"{_ICON[check['status']]} {check['name']}: {check['message']}")
        print()
        print("=" * 70)
        print("RESULTS PER VARIABLE")
        print("=" * 70)
        for name, status in summary["variables"].items():
            print(f"{name}: {'OK' if status == 'ok' else 'FAILED (see QA report)'}")
        c = summary["counts"]
        n_ok = sum(1 for s in summary["variables"].values() if s == "ok")
        print(
            f"\nTotal: {c['fail']} fail, {c['warn']} warn, {n_ok}/{len(summary['variables'])} variables succeeded."
        )
    if summary["run_dir"]:
        print(f"Outputs written to: {summary['run_dir']}")
    if summary["error"]:
        print(f"[FAIL] {summary['error']}", file=sys.stderr)


def _cmd_run(args) -> int:
    if args.json:
        summary = service.run_config_file(args.config, on_event=_emit_json)
        _emit_json({"event": "run_finished", **summary})
    else:
        summary = service.run_config_file(args.config)
        _print_run_report(summary)
    return summary["exit_code"]


def _cmd_validate(args) -> int:
    result = service.validate_config_file(args.config)
    if args.json:
        _emit_json(result)
        return 0 if result["valid"] else 1
    for e in result["errors"]:
        where = ".".join(str(x) for x in e["loc"])
        who = f"{e['variable']}: " if e["variable"] else ""
        print(f"[FAIL] {who}{where + ': ' if where else ''}{e['message']}")
    for w in result.get("warnings", []):
        print(f"[WARN] {w['variable']}: {w['message']}")
    if result["valid"]:
        n_warn = len(result.get("warnings", []))
        print(
            f"[PASS] {args.config}: valid ({len(result['variables'])} variables"
            + (f", {n_warn} warning(s))." if n_warn else ").")
        )
    return 0 if result["valid"] else 1


def _cmd_inspect(args) -> int:
    try:
        info = service.inspect_path(args.path)
    except (OSError, ValueError) as exc:
        if args.json:
            _emit_json({"error": str(exc)})
        else:
            print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    (_emit_json if args.json else _print_pretty)(info)
    return 0


def _cmd_methods(args) -> int:
    methods = service.list_methods()
    if args.json:
        _emit_json(methods)
    else:
        for m in methods:
            print(f"{m['name']:<30} {m['summary']}")
    return 0


def _cmd_schema(args) -> int:
    _print_pretty(service.config_json_schema())
    return 0


_COMMANDS = {
    "run": _cmd_run,
    "validate": _cmd_validate,
    "inspect": _cmd_inspect,
    "methods": _cmd_methods,
    "schema": _cmd_schema,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bcflow", description="Config-driven bias correction built on xsdba."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {service.bcflow_version()}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    json_help = "machine-readable JSON on stdout (for the desktop app)"
    run = sub.add_parser("run", help="run the pipeline and write the outputs")
    run.add_argument("config", type=Path, help="path to a YAML or JSON config file")
    run.add_argument("--json", action="store_true", help=json_help)

    validate = sub.add_parser("validate", help="check a config without running it")
    validate.add_argument(
        "config", type=Path, help="path to a YAML or JSON config file"
    )
    validate.add_argument("--json", action="store_true", help=json_help)

    inspect = sub.add_parser("inspect", help="describe a data file or folder")
    inspect.add_argument(
        "path", type=Path, help=".nc, .zarr, .csv or a folder of station .csv files"
    )
    inspect.add_argument("--json", action="store_true", help=json_help)

    methods = sub.add_parser("methods", help="list the available correction methods")
    methods.add_argument("--json", action="store_true", help=json_help)

    sub.add_parser("schema", help="print the JSON Schema of config.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
