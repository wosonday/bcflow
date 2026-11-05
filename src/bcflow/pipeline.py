"""Orchestrate running every variable declared in a config.

This is the ONLY place that decides the running POLICY across variables: one
variable's failure (validation or runtime) must not stop the others. It knows
nothing about HOW a single variable gets bias-corrected -- that is
adjust.py's job. Kept separate so changing the policy later (e.g. running
variables in parallel, retrying a failed one) never needs to touch the
train/adjust logic, and vice versa.
"""

from __future__ import annotations

from typing import Any, Callable

from .adjust import run_variable
from .config_schema import PipelineConfig, VariableConfig
from .qa import QACheck

EventHandler = Callable[[dict], None]


def _name_of(raw: Any) -> str:
    if isinstance(raw, VariableConfig):
        return raw.name
    if isinstance(raw, dict) and raw.get("name"):
        return str(raw["name"])
    return "<unnamed>"


def run_pipeline(config: PipelineConfig | dict, on_event: EventHandler | None = None) -> dict:
    """Run every variable in config['variables'] (or config.variables).

    IMPORTANT: each variable is validated (pydantic VariableConfig) AND run
    INSIDE the same per-variable try/except -- the whole PipelineConfig is
    never validated up front. Validating everything first would mean a
    configuration error in variable 'pr' (e.g. missing adapt_freq_thresh)
    raises BEFORE variable 'tasmax' gets a chance to run at all, violating
    the exact per-variable error-isolation guarantee this module exists for.

    Errors that still raise straight through (whole-file structural errors,
    not any single variable's fault): no 'variables' key or an empty list,
    and duplicated variable names (results are keyed by name).

    `on_event`, if given, receives a plain dict at each step so a caller can
    show live progress (the CLI's --json mode prints them as JSON lines):
        {"event": "run_started", "variables": [name, ...]}
        {"event": "variable_started", "variable": name, "index": i, "total": n}
        {"event": "qa", "variable": name, "check": {...}}   one per new QACheck
        {"event": "variable_finished", "variable": name, "status": "ok" | "failed"}

    Returns {"results": {name: {"scen", "adj", "evaluation"} | None}, "qa": [...]}.
    """
    emit = on_event or (lambda event: None)

    if isinstance(config, PipelineConfig):
        raw_variables: list[Any] = list(config.variables)
    else:
        raw_variables = config.get("variables") or []
        if not raw_variables:
            raise ValueError("config: the 'variables' key is missing or the list is empty.")

    names = [_name_of(raw) for raw in raw_variables]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise ValueError(f"config: variable names must be unique; duplicated: {duplicated}.")

    qa: list[QACheck] = []
    results: dict[str, dict | None] = {}
    emit({"event": "run_started", "variables": names})

    for index, (name, raw) in enumerate(zip(names, raw_variables), start=1):
        emit({"event": "variable_started", "variable": name, "index": index, "total": len(names)})
        first_new = len(qa)
        try:
            var_cfg = raw if isinstance(raw, VariableConfig) else VariableConfig.model_validate(raw)
            results[name] = run_variable(var_cfg, qa)
        except Exception as exc:  # isolates both validation and runtime errors, per variable
            qa.append(QACheck(
                name=f"pipeline:{name}:exception",
                status="fail",
                message=f"Variable '{name}' failed: {exc}",
                context={"variable": name},
            ))
            results[name] = None

        for check in qa[first_new:]:
            emit({"event": "qa", "variable": name, "check": check.as_dict()})
        emit({
            "event": "variable_finished",
            "variable": name,
            "status": "ok" if results[name] is not None else "failed",
        })

    return {"results": results, "qa": qa}
