"""Orchestrate running every variable declared in a config.

This is the ONLY place that decides the running POLICY across variables:

- the config is validated ONCE, as a whole, before anything runs. Its
  sections are shared (stations, model, periods), so a mistake there is no
  single variable's fault, and fixing it before a long run is cheaper;
- at RUN time, one variable's failure (missing data, units, xsdba error)
  must not stop the others.

It knows nothing about HOW a single variable gets bias-corrected -- that is
adjust.py's job.
"""

from __future__ import annotations

from .adjust import run_variable
from .config_schema import PipelineConfig, load_config
from .progress import EventHandler, RunControl
from .qa import QACheck


def run_pipeline(
    config: PipelineConfig | dict,
    on_event: EventHandler | None = None,
    *,
    control: RunControl | None = None,
) -> dict:
    """Run every variable in config.variables.

    Raises ValueError (pydantic's ValidationError is one) if the config is
    invalid; nothing has run at that point.

    `on_event`, if given, receives a plain dict at each step so a caller can
    show live progress (the CLI's --json mode prints them as JSON lines):
        {"event": "run_started", "variables": [name, ...]}
        {"event": "variable_started", "variable": name, "index": i, "total": n}
        {"event": "progress", "variable": name, "stage": stage, ...}
        {"event": "qa", "variable": name, "check": {...}}   one per new QACheck
        {"event": "variable_finished", "variable": name, "status": status}
        {"event": "run_cancelled", ...}   on interruption

    Pass a RunControl to request cancellation between expensive stages. An explicit
    on_event overrides the control's callback. Cancellation retains completed
    results, omits the active variable's partial results, and skips later variables.

    Returns results, qa, cancelled, and variable_statuses (ok/failed/cancelled/skipped).
    """
    control = control or RunControl()
    if on_event is not None:
        control.on_event = on_event
    emit = control.emit
    cfg = config if isinstance(config, PipelineConfig) else load_config(config)
    names = [str(v) for v in cfg.variables]

    qa: list[QACheck] = []
    results: dict[str, dict | None] = {}
    emit({"event": "run_started", "variables": names})

    statuses = dict.fromkeys(names, "skipped")
    active: str | None = None
    first_new = 0
    try:
        control.check_cancelled()
        for index, name in enumerate(names, start=1):
            control.check_cancelled()
            active = name
            first_new = len(qa)
            emit(
                {
                    "event": "variable_started",
                    "variable": name,
                    "index": index,
                    "total": len(names),
                }
            )
            control.check_cancelled()
            try:
                results[name] = run_variable(cfg, name, qa, control=control)
            except Exception as exc:  # isolates runtime errors, per variable
                qa.append(
                    QACheck(
                        name=f"pipeline:{name}:exception",
                        status="fail",
                        message=f"Variable '{name}' failed: {exc}",
                        context={"variable": name},
                    )
                )
                results[name] = None

            statuses[name] = "ok" if results[name] is not None else "failed"
            active = None
            for check in qa[first_new:]:
                emit({"event": "qa", "variable": name, "check": check.as_dict()})
            emit(
                {
                    "event": "variable_finished",
                    "variable": name,
                    "status": statuses[name],
                }
            )
        control.check_cancelled()
    except KeyboardInterrupt:
        control.request_cancel()
        if active is not None:
            results.pop(active, None)
            statuses[active] = "cancelled"
            for check in qa[first_new:]:
                emit({"event": "qa", "variable": active, "check": check.as_dict()})
            emit(
                {
                    "event": "variable_finished",
                    "variable": active,
                    "status": "cancelled",
                }
            )
        emit(
            {
                "event": "run_cancelled",
                "variable": active,
                "message": "Saving completed variables; unfinished work is discarded.",
            }
        )

    return {
        "results": results,
        "qa": qa,
        "cancelled": control.cancelled,
        "variable_statuses": statuses,
    }
