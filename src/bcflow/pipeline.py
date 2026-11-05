"""Orchestrate running every variable declared in a config.

This is the ONLY place that decides the running POLICY across variables: one
variable's failure (validation or runtime) must not stop the others. It knows
nothing about HOW a single variable gets bias-corrected -- that is
adjust.py's job. Kept separate so changing the policy later (e.g. running
variables in parallel, retrying a failed one) never needs to touch the
train/adjust logic, and vice versa.
"""

from __future__ import annotations

from .adjust import run_variable
from .config_schema import PipelineConfig, VariableConfig
from .qa import QACheck


def run_pipeline(config: PipelineConfig | dict) -> dict:
    """Run every variable in config['variables'] (or config.variables).

    IMPORTANT: each variable is validated (pydantic VariableConfig) AND run
    INSIDE the same per-variable try/except -- the whole PipelineConfig is
    never validated up front. Validating everything first would mean a
    configuration error in variable 'pr' (e.g. missing adapt_freq_thresh)
    raises BEFORE variable 'tasmax' gets a chance to run at all, violating
    the exact per-variable error-isolation guarantee this module exists for.

    The only error that still raises straight through (cannot be isolated to
    one variable): the config has no 'variables' key at all, or an empty
    list -- a whole-file structural error, not any single variable's fault.

    Returns {"results": {var_name: {"scen":..., "adj":...} | None}, "qa": [...]}.
    """
    if isinstance(config, PipelineConfig):
        raw_variables: list[dict | VariableConfig] = list(config.variables)
    else:
        raw_variables = config.get("variables") or []
        if not raw_variables:
            raise ValueError("config: the 'variables' key is missing or the list is empty.")

    qa: list[QACheck] = []
    results: dict[str, dict | None] = {}

    for raw in raw_variables:
        name = raw.name if isinstance(raw, VariableConfig) else raw.get("name", "<không rõ tên>")
        try:
            var_cfg = raw if isinstance(raw, VariableConfig) else VariableConfig.model_validate(raw)
            scen, adj = run_variable(var_cfg, qa)
            results[name] = {"scen": scen, "adj": adj}
        except Exception as exc:  # isolates both validation and runtime errors, per variable
            qa.append(QACheck(
                name=f"pipeline:{name}:exception",
                status="fail",
                message=f"Variable '{name}' failed: {exc}",
            ))
            results[name] = None

    return {"results": results, "qa": qa}