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

from collections.abc import Callable

from .adjust import run_variable
from .config_schema import PipelineConfig, load_config
from .qa import QACheck

EventHandler = Callable[[dict], None]


def run_pipeline(config: PipelineConfig | dict, on_event: EventHandler | None = None) -> dict:
    """Run every variable in config.variables.

    Raises ValueError (pydantic's ValidationError is one) if the config is
    invalid; nothing has run at that point.

    `on_event`, if given, receives a plain dict at each step so a caller can
    show live progress (the CLI's --json mode prints them as JSON lines):
        {"event": "run_started", "variables": [name, ...]}
        {"event": "variable_started", "variable": name, "index": i, "total": n}
        {"event": "qa", "variable": name, "check": {...}}   one per new QACheck
        {"event": "variable_finished", "variable": name, "status": "ok" | "failed"}

    Returns {"results": {name: {"scen", "adj", "evaluation", "hist_eval"} | None}, "qa": [...]}.
    """
    emit = on_event or (lambda event: None)
    cfg = config if isinstance(config, PipelineConfig) else load_config(config)
    names = [str(v) for v in cfg.variables]

    qa: list[QACheck] = []
    results: dict[str, dict | None] = {}
    emit({"event": "run_started", "variables": names})

    for index, name in enumerate(names, start=1):
        emit({"event": "variable_started", "variable": name, "index": index, "total": len(names)})
        first_new = len(qa)
        try:
            results[name] = run_variable(cfg, name, qa)
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

        for check in qa[first_new:]:
            emit({"event": "qa", "variable": name, "check": check.as_dict()})
        emit(
            {
                "event": "variable_finished",
                "variable": name,
                "status": "ok" if results[name] is not None else "failed",
            }
        )

    return {"results": results, "qa": qa}
