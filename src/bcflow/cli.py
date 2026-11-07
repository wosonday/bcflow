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
    130 cancelled (completed variables are saved unless a second Ctrl+C forces exit)
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

import numpy as np
from tqdm import tqdm

from . import service
from .progress import RunControl

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
    if summary["variables"]:
        print("=" * 70)
        print("RESULTS PER VARIABLE")
        print("=" * 70)
        for name, status in summary["variables"].items():
            print(f"{name}: {status.upper()}")
        c = summary["counts"]
        n_ok = sum(1 for s in summary["variables"].values() if s == "ok")
        print(
            f"\nTotal: {c['fail']} fail, {c['warn']} warn, {n_ok}/{len(summary['variables'])} variables succeeded."
        )
    if summary["status"] == "cancelled":
        print(
            "Run cancelled. Unfinished work was discarded."
            + (" Fully completed variables were saved." if summary["run_dir"] else "")
        )
    if summary["run_dir"]:
        print(f"Outputs written to: {summary['run_dir']}")
    if summary["error"]:
        print(f"[FAIL] {summary['error']}", file=sys.stderr)


def _print_progress(event: dict) -> None:
    kind = event["event"]
    if kind == "run_started":
        text = "Starting run. Press Ctrl+C to cancel and save completed variables."
    elif kind == "variable_started":
        text = f"[{event['index']}/{event['total']}] Starting {event['variable']}"
    elif kind == "variable_finished":
        text = f"{event['variable']}: {event['status'].upper()}"
    elif kind == "run_cancelled":
        text = event["message"]
    elif kind == "progress":
        text = f"{event['variable']}: {event['stage'].replace('_', ' ')}"
        if "source" in event:
            text += f" ({event['source']})"
        if event["stage"] == "worker_selection":
            text += (
                f" | {event['workers']} worker(s), CPU limit {event['cpu_limit']} (CPU count - 1)"
                f" | RAM limit {event['memory_limit']}"
                f" | estimated {event['estimated_task_bytes'] / 1024**3:.2f} GiB/task"
                f" | budget {event['budget_bytes'] / 1024**3:.2f} GiB"
            )
            if event["over_budget"]:
                text += " | WARNING: one block exceeds budget; consider a smaller block_size"
        if "block_index" in event:
            completed, total = event["completed_cells"], event["total_cells"]
            text += (
                f" | block {event['block_index']}/{event['total_blocks']}"
                f" | {completed}/{total} cells ({completed / total:.1%})"
            )
        elif "completed" in event:
            text += f" | {event['completed']}/{event['total']} {event['unit']}"
    else:
        return
    print(
        f"[{event.get('elapsed_seconds', 0):.1f}s] {text}", file=sys.stderr, flush=True
    )


class _TerminalProgress:
    """Render existing events as bars only when stderr is an interactive terminal."""

    def __init__(self):
        self.interactive = sys.stderr.isatty()
        self.bar = None
        self.key: tuple[str, str] | None = None

    def close(self) -> None:
        if self.bar is not None:
            self.bar.close()
            self.bar = None
        self.key = None

    def __call__(self, event: dict) -> None:
        if not self.interactive:
            _print_progress(event)
            return
        kind = event["event"]
        if kind != "progress":
            if kind in {"variable_started", "variable_finished", "run_cancelled"}:
                self.close()
            _print_progress(event)
            return

        variable, stage = event["variable"], event["stage"]
        if stage == "worker_selection" and self.bar is not None:
            self.bar.set_postfix_str(
                f"{event['workers']} worker(s) | CPU limit {event['cpu_limit']} | RAM limit {event['memory_limit']}"
                + (" | RAM budget exceeded" if event["over_budget"] else "")
            )
            return
        if "block_index" in event:
            key = (variable, "cells")
            total, completed, unit = (
                event["total_cells"],
                event["completed_cells"],
                "cell",
            )
            detail = f"block {event['block_index']}/{event['total_blocks']} | {stage.replace('_', ' ')}"
        elif stage == "scanning_reference":
            key = (variable, "scan")
            total, completed, unit = event["total"], event["completed"], "step"
            detail = "scanning reference"
        else:
            self.close()
            _print_progress(event)
            return

        if self.key != key:
            self.close()
            self.bar = tqdm(
                total=total,
                desc=variable,
                unit=unit,
                file=sys.stderr,
                dynamic_ncols=True,
                mininterval=0.2,
            )
            self.key = key
        if self.bar is not None:
            if "source" in event:
                detail += f" ({event['source']})"
            self.bar.set_postfix_str(detail, refresh=False)
            # Stages change the label, but only completed blocks advance the bar.
            self.bar.update(max(0, completed - self.bar.n))
            self.bar.refresh()


class _ForcedCancellation(BaseException):
    """Bypass pipeline recovery when the user explicitly forces termination."""


def _cmd_run(args) -> int:
    progress = None if args.json else _TerminalProgress()
    control = RunControl(
        _emit_json if args.json else progress, cancel_file=args.cancel_file
    )

    def cancel_run(_signum, _frame):
        if control.cancelled:
            raise _ForcedCancellation()
        control.request_cancel()
        print(
            "Cancellation requested. Waiting for the current operation, then saving completed variables. "
            "Press Ctrl+C again to force exit (outputs may be incomplete).",
            file=sys.stderr,
            flush=True,
        )

    # signal.signal is only legal in the main thread; library callers can use RunControl.
    install_handler = threading.current_thread() is threading.main_thread()
    previous_handler = (
        signal.signal(signal.SIGINT, cancel_run) if install_handler else None
    )
    try:
        try:
            summary = service.run_config_file(args.config, control=control)
        except KeyboardInterrupt, _ForcedCancellation:
            print(
                "Forced cancellation. Output files may be incomplete.",
                file=sys.stderr,
                flush=True,
            )
            if args.json:
                _emit_json(
                    {
                        "event": "run_finished",
                        "status": "cancelled",
                        "exit_code": 130,
                        "forced": True,
                        "error": "Interrupted before results could be safely saved.",
                        "run_dir": None,
                        "manifest": None,
                        "variables": {},
                        "counts": {"pass": 0, "warn": 0, "fail": 0},
                        "qa": [],
                    }
                )
            return 130
        if progress is not None:
            progress.close()
        if args.json:
            _emit_json({"event": "run_finished", **summary})
        else:
            _print_run_report(summary)
        return summary["exit_code"]
    finally:
        if progress is not None:
            progress.close()
        if install_handler:
            signal.signal(signal.SIGINT, previous_handler)


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
    run.add_argument(
        "--cancel-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="cancel the run (saving completed variables) as soon as this file exists; "
        "for a GUI that cannot send Ctrl+C",
    )

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
