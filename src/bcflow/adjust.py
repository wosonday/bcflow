"""Run the train -> adjust pipeline for ONE variable.

This is the "adjust" layer: it assembles data (loaders), spatial dimension
resolution + nearest-point extraction (registry), the xsdba Grouper
(grouping), and the method registry (registry) into a single correction run
for one variable. It knows nothing about running several variables, or about
isolating one variable's failure from another's -- that is pipeline.py's job,
kept separate on purpose.
"""

from __future__ import annotations

import xsdba

from .config_schema import VariableConfig
from .grouping import build_grouper
from .loaders import load_variable_sources
from .qa import QACheck
from .extract import extract_nearest
from .methods import METHOD_REGISTRY


def run_variable(var_cfg: VariableConfig, qa: list[QACheck]):
    """Run bias correction for one variable. Returns (scen, adj)."""
    name = var_cfg.name

    ref, hist_raw, sim_raw, grid_cfg = load_variable_sources(var_cfg, qa)

    hist = extract_nearest(hist_raw, ref, grid_cfg, qa, source_label=f"{name}:hist")
    sim = extract_nearest(sim_raw, ref, grid_cfg, qa, source_label=f"{name}:sim")

    group = build_grouper(var_cfg)

    if var_cfg.adapt_freq:
        # adapt_freq_thresh is already guaranteed to exist by pydantic (see
        # config_schema.py), so there is nothing to re-check here -- reaching
        # this point without it means run_variable() was called directly on
        # an un-validated object, a caller bug rather than a user error.
        hist, _pth, _dP0 = xsdba.processing.adapt_freq(
            ref, hist, thresh=var_cfg.adapt_freq_thresh, group=group
        )
        qa.append(QACheck(
            name=f"adapt_freq:{name}:applied",
            status="pass",
            message=f"Completed frequency adaptation for '{name}' (thresh={var_cfg.adapt_freq_thresh}).",
        ))

    if var_cfg.method not in METHOD_REGISTRY:
        qa.append(QACheck(
            name=f"method:{name}:unknown",
            status="fail",
            message=f"Method '{var_cfg.method}' is not registered. Valid methods: {sorted(METHOD_REGISTRY)}.",
        ))
        raise ValueError(f"Invalid method for variable '{name}': {var_cfg.method!r}")

    AdjClass = METHOD_REGISTRY[var_cfg.method]
    train_kwargs = {**var_cfg.train_params, "group": group, "kind": var_cfg.kind}

    adj = AdjClass.train(ref, hist, **train_kwargs)
    scen = adj.adjust(sim, **var_cfg.adjust_params)
    # xsdba's output dim order depends on Python's per-process string hashing
    # (it builds dims from a set), so fix it: identical files on every run.
    scen = scen.transpose("time", ...)

    qa.append(QACheck(
        name=f"train_adjust:{name}:done",
        status="pass",
        message=f"Completed train+adjust for '{name}' using '{var_cfg.method}'.",
        context={"n_stations": ref.sizes.get("station", 0)},
    ))
    return scen, adj