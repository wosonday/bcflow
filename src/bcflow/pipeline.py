"""Orchestrate every target declared or derived from a config.

This is the ONLY place that decides the running POLICY across variables:

- the config is validated ONCE, as a whole, before anything runs. Its
  sections are shared (stations, model, periods), so a mistake there is no
  single variable's fault, and fixing it before a long run is cheaper;
- at RUN time, one target's failure (missing data, units, xsdba error)
  must not stop the others.

It knows nothing about HOW a single variable gets bias-corrected -- that is
adjust.py's job.
"""

from __future__ import annotations

from .adjust import run_target, run_variable
from .config_schema import PipelineConfig, load_config
from .progress import EventHandler, RunControl
from .qa import QACheck


def run_pipeline(
    config: PipelineConfig | dict,
    on_event: EventHandler | None = None,
    *,
    control: RunControl | None = None,
) -> dict:
    """Run configured targets, or one legacy correction per variable.

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
    results, omits the active target's partial results, and skips later targets.

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
    var_index = 0
    active_vars: list[str] = []
    first_new = 0

    def emit_qa(variables: list[str], target_name: str) -> None:
        for check in qa[first_new:]:
            variable = (
                variables[0]
                if len(variables) == 1
                else check.context.get("variable") or target_name
            )
            emit({"event": "qa", "variable": variable, "check": check.as_dict()})

    try:
        control.check_cancelled()
        for target in cfg.resolved_targets():
            control.check_cancelled()
            t_vars = [str(v) for v in target.variables]
            active_vars = t_vars
            first_new = len(qa)
            for name in t_vars:
                var_index += 1
                emit(
                    {
                        "event": "variable_started",
                        "variable": name,
                        "index": var_index,
                        "total": len(names),
                    }
                )
            control.check_cancelled()
            try:
                if cfg.targets is None:
                    target_results = {
                        t_vars[0]: run_variable(cfg, t_vars[0], qa, control=control)
                    }
                else:
                    target_results = run_target(cfg, target, qa, control=control)
                results.update(target_results)
            except Exception as exc:  # isolates runtime errors, per target
                if cfg.targets is None:
                    check = QACheck(
                        name=f"pipeline:{t_vars[0]}:exception",
                        status="fail",
                        message=f"Variable '{t_vars[0]}' failed: {exc}",
                        context={"variable": t_vars[0]},
                    )
                else:
                    check = QACheck(
                        name=f"pipeline:{target.name}:exception",
                        status="fail",
                        message=f"Target '{target.name}' failed: {exc}",
                        context={"target": target.name, "variables": t_vars},
                    )
                qa.append(check)
                for name in t_vars:
                    results[name] = None

            for name in t_vars:
                statuses[name] = "ok" if results[name] is not None else "failed"
            active_vars = []
            emit_qa(t_vars, target.name)
            for name in t_vars:
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
        if active_vars:
            for name in active_vars:
                results.pop(name, None)
                statuses[name] = "cancelled"
            emit_qa(active_vars, target.name)
            for name in active_vars:
                emit(
                    {
                        "event": "variable_finished",
                        "variable": name,
                        "status": "cancelled",
                    }
                )
        emit(
            {
                "event": "run_cancelled",
                "variable": active_vars[0] if active_vars else None,
                "message": "Saving completed variables; unfinished work is discarded.",
            }
        )

    return {
        "results": results,
        "qa": qa,
        "cancelled": control.cancelled,
        "variable_statuses": statuses,
    }
