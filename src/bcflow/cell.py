"""Method-agnostic 7-step cell execution contract for bias correction."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any

import numpy as np
import xarray as xr
import xsdba.processing
from xsdba.base import Grouper

from .grouping import build_grouper, sample_size_qa
from .methods import MethodSpec, check_available, get_method
from .preprocess import (
    SEED,
    ZeroHandling,
    apply_adapt_freq,
    clip_to_bounds,
    resolve_adapt_freq,
)
from .progress import RunControl
from .qa import QACheck
from .variables import VariableSpec, resolve_method_params

STACK_DIM = "multivar"


@dataclass(frozen=True)
class PostStep:
    """Configuration for a post-processing step applied after primary adjustment."""

    method: str
    train_params: tuple[tuple[str, Any], ...] = ()
    adjust_params: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class CellPlan:
    """Pickle-safe execution plan for one target cell/block."""

    target_name: str
    variables: tuple[str, ...]
    method: str
    group: str = "month"
    window: int = 1
    pool_dims: tuple[str, ...] = ()
    train_params: tuple[tuple[str, Any], ...] = ()
    adjust_params: tuple[tuple[str, Any], ...] = ()
    post: tuple[PostStep, ...] = ()
    has_projection: bool = True


@dataclass
class CellResult:
    """Pickle-safe result from executing a CellPlan (strip trained_obj, zeros, and post_trained before IPC)."""

    scen: xr.Dataset
    trained_ds: xr.Dataset | None
    qa: list[QACheck]
    timing: dict[str, float]
    clip_counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    trained_obj: Any | None = None
    zeros: ZeroHandling | None = None
    post_trained: tuple[tuple[MethodSpec, Any, dict[str, Any]], ...] = ()


def _validate(
    plan: CellPlan,
    specs: dict[str, VariableSpec],
    qa: list[QACheck],
) -> MethodSpec:
    """Step 1: validate method availability, family, projection support, and arity."""
    method = get_method(plan.method)
    check_available(method)

    if method.family == "post":
        qa.append(
            QACheck(
                name=f"method:{plan.target_name}:invalid_family",
                status="fail",
                message=(
                    f"Method '{plan.method}' has family 'post' and cannot be used "
                    "as a primary method."
                ),
                context={
                    "target": plan.target_name,
                    "method": plan.method,
                    "family": method.family,
                },
            )
        )
        raise ValueError(
            f"Method '{plan.method}' has family 'post' and cannot be used as a primary method."
        )

    if plan.has_projection and not method.supports_projection:
        qa.append(
            QACheck(
                name=f"method:{plan.target_name}:no_projection",
                status="fail",
                message=(
                    f"Method '{plan.method}' does not support projection onto future 'sim'."
                ),
                context={"target": plan.target_name, "method": plan.method},
            )
        )
        raise ValueError(
            f"Method '{plan.method}' does not support projection onto future 'sim'."
        )

    if len(plan.variables) < method.min_vars:
        qa.append(
            QACheck(
                name=f"method:{plan.target_name}:min_vars",
                status="fail",
                message=(
                    f"Method '{plan.method}' requires at least {method.min_vars} "
                    f"variable(s), got {len(plan.variables)}."
                ),
                context={
                    "target": plan.target_name,
                    "method": plan.method,
                    "min_vars": method.min_vars,
                    "n_vars": len(plan.variables),
                },
            )
        )
        raise ValueError(
            f"Method '{plan.method}' requires at least {method.min_vars} variable(s), "
            f"got {len(plan.variables)}."
        )

    missing_specs = [v for v in plan.variables if v not in specs]
    if missing_specs:
        raise ValueError(
            f"Missing VariableSpec for variable(s) {missing_specs} in target '{plan.target_name}'."
        )

    return method


def _to_tensor(
    ds: xr.Dataset,
    variables: tuple[str, ...],
    *,
    multivariate: bool,
    units: dict[str, str] | None = None,
) -> xr.DataArray:
    """Step 2: select a univariate DataArray or stack variables along STACK_DIM."""
    sub = ds[list(variables)].copy(deep=False)
    if units:
        for v in variables:
            if "units" not in sub[v].attrs and v in units and units[v]:
                sub[v] = sub[v].assign_attrs(units=units[v])

    if multivariate:
        return xsdba.processing.stack_variables(sub, dim=STACK_DIM)
    return sub[variables[0]]


def _inject_registry_defaults(
    method: MethodSpec,
    variables: tuple[str, ...],
    specs: dict[str, VariableSpec],
    train_kws: dict[str, Any],
    adjust_kws: dict[str, Any],
) -> None:
    """Populate absent registry_defaults keys from VariableSpec metadata."""
    primary_spec = specs[variables[0]]
    for key in method.registry_defaults:
        if key == "kind":
            train_kws.setdefault("kind", primary_spec.kind)
        elif key == "thresh":
            if primary_spec.adapt_freq_thresh:
                train_kws.setdefault("thresh", primary_spec.adapt_freq_thresh)
            elif "thresh" not in train_kws:
                raise ValueError(
                    f"Method '{method.name}' requires 'thresh' via train_params "
                    f"or spec.adapt_freq_thresh for '{primary_spec.name}'."
                )
        elif key == "cluster_thresh":
            if primary_spec.adapt_freq_thresh:
                train_kws.setdefault("cluster_thresh", primary_spec.adapt_freq_thresh)
            elif "cluster_thresh" not in train_kws:
                raise ValueError(
                    f"Method '{method.name}' requires 'cluster_thresh' via train_params "
                    f"or spec.adapt_freq_thresh for '{primary_spec.name}'."
                )
        elif key == "crd_dim":
            train_kws.setdefault("crd_dim", STACK_DIM)
        elif key == "base_kws_vars":
            adjust_kws.setdefault(
                "base_kws_vars",
                {v: {"kind": specs[v].kind} for v in variables},
            )


def _fill_registry_defaults(
    plan: CellPlan,
    method: MethodSpec,
    specs: dict[str, VariableSpec],
    ref_t: xr.DataArray,
    qa: list[QACheck],
    *,
    emit_sample_size_qa: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], Grouper]:
    """Step 3: resolve params, inject registry_defaults, build Grouper, and emit sample-size QA."""
    if method.min_vars > 1:
        # Never leak univariate VariableSpec defaults (e.g. precip's interp/nquantiles)
        # into multivariate methods such as mbcn, principal_components, or npdf_transform.
        train_kws = {**dict(method.default_train_params), **dict(plan.train_params)}
        adjust_kws = {**dict(method.default_adjust_params), **dict(plan.adjust_params)}
    else:
        primary_spec = specs[plan.variables[0]]
        train_kws, adjust_kws = resolve_method_params(
            primary_spec,
            method.meta,
            explicit_train=dict(plan.train_params),
            explicit_adjust=dict(plan.adjust_params),
        )
    _inject_registry_defaults(method, plan.variables, specs, train_kws, adjust_kws)

    grouper = build_grouper(plan.group, window=plan.window, pool_dims=plan.pool_dims)

    if emit_sample_size_qa and "nquantiles" in train_kws:
        nq_val = train_kws["nquantiles"]
        nquantiles = (
            int(nq_val) if isinstance(nq_val, (int, np.integer)) else len(nq_val)
        )
        n_years = max(0, round(ref_t.sizes["time"] / 365.25))
        qa.append(
            sample_size_qa(
                plan.group,
                plan.window,
                n_years,
                nquantiles,
                variable=plan.target_name,
            )
        )

    return train_kws, adjust_kws, grouper


def _preprocess(
    ref_t: xr.DataArray,
    hist_t: xr.DataArray,
    *,
    plan: CellPlan,
    method: MethodSpec,
    spec: VariableSpec,
    units: dict[str, str],
    grouper: Grouper,
    train_kws: dict[str, Any],
    adjust_kws: dict[str, Any],
    qa: list[QACheck],
    seed: int,
    multivariate: bool,
) -> tuple[xr.DataArray, xr.DataArray, ZeroHandling | None]:
    """Step 4: univariate quantile-gated jitter, hist-only adapt_freq, and group attachment."""
    zeros: ZeroHandling | None = None

    if not multivariate:
        name = plan.variables[0]
        if spec.jitter_under_thresh and method.uses_quantiles:
            zeros = ZeroHandling(spec, units[name], seed=seed)
            ref_t = zeros.jitter(ref_t)
            hist_t = zeros.jitter(hist_t)
            qa.append(
                QACheck(
                    name=f"preprocess:{name}:jitter",
                    status="pass",
                    message=(
                        f"Applied seeded zero-jitter below '{spec.jitter_under_thresh}' "
                        f"for '{name}'."
                    ),
                    context={
                        "variable": name,
                        "thresh": spec.jitter_under_thresh,
                        "seed": seed,
                    },
                )
            )

        thresh, is_native = resolve_adapt_freq(spec, method.meta, train_kws)
        if thresh and not is_native:
            hist_t, check = apply_adapt_freq(
                ref_t, hist_t, thresh, grouper, variable=name, seed=seed
            )
            qa.append(check)
        elif thresh and is_native:
            qa.append(
                QACheck(
                    name=f"adapt_freq:{name}:applied",
                    status="pass",
                    message=(
                        f"Using native frequency adaptation for '{name}' "
                        f"(thresh={thresh})."
                    ),
                    context={"variable": name, "thresh": thresh, "native": True},
                )
            )

    if method.family == "train_adjust":
        if method.name == "mbcn":
            base_kws = dict(train_kws.get("base_kws") or {})
            base_kws.setdefault("group", grouper)
            train_kws["base_kws"] = base_kws
            train_kws.setdefault("pts_dim", STACK_DIM)
        else:
            train_kws.setdefault("group", grouper)
    elif method.family == "adjust_only":
        if method.name == "npdf_transform":
            base_kws = dict(adjust_kws.get("base_kws") or {})
            base_kws.setdefault("group", grouper)
            adjust_kws["base_kws"] = base_kws
            adjust_kws.setdefault("pts_dim", STACK_DIM)
        else:
            adjust_kws.setdefault("group", grouper)
            adjust_kws.setdefault("pts_dim", STACK_DIM)

    return ref_t, hist_t, zeros


def _train(
    method: MethodSpec,
    ref_t: xr.DataArray,
    hist_t: xr.DataArray,
    train_kws: dict[str, Any],
    *,
    seed: int = SEED,
) -> tuple[Any | None, xr.Dataset | None]:
    """Step 5: fit train_adjust methods; return (None, None) for adjust_only."""
    if method.family == "train_adjust":
        if method.name == "mbcn":
            # xsdba's random rotations draw from NumPy's global generator.
            np.random.seed(seed)
        trained = method.cls.train(ref_t, hist_t, **train_kws)
        return trained, trained.ds
    return None, None


def _has_string_non_time_coord(da: xr.DataArray) -> bool:
    """Return True when any non-time coordinate has string or object dtype."""
    return any(
        name != "time" and coord.dtype.kind in ("U", "S", "O", "T")
        for name, coord in da.coords.items()
    )


def _adjust(
    method: MethodSpec,
    trained: Any | None,
    ref_t: xr.DataArray,
    hist_t: xr.DataArray,
    sim_t: xr.DataArray,
    adjust_kws: dict[str, Any],
    zeros: ZeroHandling | None,
    *,
    seed: int = SEED,
) -> tuple[xr.DataArray, xr.DataArray]:
    """Step 6: jitter sim (if active), run adjust, and unwrap NpdfTransform Datasets."""
    if zeros is not None:
        sim_t = zeros.jitter(sim_t)

    # xsdba's LOCI defaults interp="linear", which raises UFuncTypeError in
    # xsdba.utils.broadcast on string station coordinates unless overridden.
    if (
        method.name == "loci"
        and "interp" not in adjust_kws
        and _has_string_non_time_coord(sim_t)
    ):
        adjust_kws = {**adjust_kws, "interp": "nearest"}

    if method.family == "train_adjust":
        if method.name == "mbcn":
            np.random.seed(seed)
            scen_t = trained.adjust(sim_t, ref_t, hist_t, **adjust_kws)
        else:
            scen_t = trained.adjust(sim_t, **adjust_kws)
    elif method.supports_projection:
        if method.name == "npdf_transform":
            np.random.seed(seed)
        scen_t = method.cls.adjust(ref_t, hist_t, sim_t, **adjust_kws)
    else:
        scen_t = method.cls.adjust(ref_t, hist_t, **adjust_kws)

    if isinstance(scen_t, xr.Dataset) and "scen" in scen_t:
        scen_t = scen_t["scen"]

    return scen_t, sim_t


def _finalize(
    scen_t: xr.DataArray,
    *,
    plan: CellPlan,
    ref_t: xr.DataArray,
    hist_t: xr.DataArray,
    sim_t: xr.DataArray,
    specs: dict[str, VariableSpec],
    units: dict[str, str],
    zeros: ZeroHandling | None,
    multivariate: bool,
    qa: list[QACheck],
    fitted_post: tuple[tuple[MethodSpec, Any, dict[str, Any]], ...] | None = None,
) -> tuple[
    xr.Dataset,
    dict[str, tuple[int, int]],
    tuple[tuple[MethodSpec, Any, dict[str, Any]], ...],
]:
    """Step 7: apply post steps -> unstack -> restore zeros -> preserve units -> clip -> transpose."""
    trained_steps: list[tuple[MethodSpec, Any, dict[str, Any]]] = []
    if fitted_post is not None:
        for post_spec, post_obj, post_adjust in fitted_post:
            scen_t = post_obj.adjust(sim_t, scen_t, **post_adjust)
            trained_steps.append((post_spec, post_obj, post_adjust))
            qa.append(
                QACheck(
                    name=f"post:{plan.target_name}:{post_spec.name}",
                    status="pass",
                    message=(
                        f"Applied post-processing '{post_spec.name}' for '{plan.target_name}'."
                    ),
                    context={"target": plan.target_name, "method": post_spec.name},
                )
            )
    else:
        for step in plan.post:
            post_spec = get_method(step.method)
            if post_spec.family != "post":
                qa.append(
                    QACheck(
                        name=f"post:{plan.target_name}:invalid_family",
                        status="fail",
                        message=(
                            f"Post-step method '{step.method}' must have family 'post', "
                            f"got '{post_spec.family}'."
                        ),
                        context={
                            "target": plan.target_name,
                            "method": step.method,
                            "family": post_spec.family,
                        },
                    )
                )
                raise ValueError(
                    f"Post-step method '{step.method}' must have family 'post', "
                    f"got '{post_spec.family}'."
                )
            check_available(post_spec)

            post_train = {
                **dict(post_spec.default_train_params),
                **dict(step.train_params),
            }
            post_adjust = {
                **dict(post_spec.default_adjust_params),
                **dict(step.adjust_params),
            }
            _inject_registry_defaults(
                post_spec, plan.variables, specs, post_train, post_adjust
            )

            post_obj = post_spec.cls.train(ref_t, hist_t, **post_train)
            scen_t = post_obj.adjust(sim_t, scen_t, **post_adjust)
            trained_steps.append((post_spec, post_obj, post_adjust))
            qa.append(
                QACheck(
                    name=f"post:{plan.target_name}:{step.method}",
                    status="pass",
                    message=(
                        f"Applied post-processing '{step.method}' for '{plan.target_name}'."
                    ),
                    context={"target": plan.target_name, "method": step.method},
                )
            )

    if multivariate:
        if "units" not in scen_t.attrs:
            scen_t = scen_t.assign_attrs(units="")
        scen = xsdba.processing.unstack_variables(scen_t, dim=STACK_DIM)
    else:
        scen = scen_t.to_dataset(name=plan.variables[0])

    if zeros is not None:
        primary_name = plan.variables[0]
        scen[primary_name] = zeros.restore(scen[primary_name])

    for v in plan.variables:
        if v in scen.data_vars and v in units and units[v]:
            scen[v].attrs["units"] = units[v]

    sub_specs = {v: specs[v] for v in plan.variables if v in specs}
    scen, clip_qa, clip_counts = clip_to_bounds(scen, sub_specs)
    qa.extend(clip_qa)

    for v in list(scen.data_vars):
        scen[v] = scen[v].transpose("time", ...)

    return scen, clip_counts, tuple(trained_steps)


def run_cell(
    plan: CellPlan,
    *,
    ref: xr.Dataset,
    hist: xr.Dataset,
    sim: xr.Dataset,
    specs: dict[str, VariableSpec],
    units: dict[str, str],
    control: RunControl | None = None,
    seed: int = SEED,
    qa: list[QACheck] | None = None,
) -> CellResult:
    """Execute the 7-step bias-correction contract on one target cell/block.

    If `qa` is given, checks are appended into that caller-owned list (so fail
    checks emitted right before a raised ValueError survive) and
    CellResult.qa is that same list; otherwise a fresh list is used.
    """
    t_start = monotonic()
    if qa is None:
        qa = []
    timing: dict[str, float] = {}

    if control is not None:
        control.check_cancelled()

    t0 = monotonic()
    method = _validate(plan, specs, qa)
    timing["validate"] = round(monotonic() - t0, 6)

    multivariate = method.min_vars > 1
    t0 = monotonic()
    ref_t = _to_tensor(ref, plan.variables, multivariate=multivariate, units=units)
    hist_t = _to_tensor(hist, plan.variables, multivariate=multivariate, units=units)
    sim_t = _to_tensor(sim, plan.variables, multivariate=multivariate, units=units)
    timing["to_tensor"] = round(monotonic() - t0, 6)

    t0 = monotonic()
    train_kws, adjust_kws, grouper = _fill_registry_defaults(
        plan, method, specs, ref_t, qa
    )
    timing["defaults"] = round(monotonic() - t0, 6)

    t0 = monotonic()
    ref_t, hist_t, zeros = _preprocess(
        ref_t,
        hist_t,
        plan=plan,
        method=method,
        spec=specs[plan.variables[0]],
        units=units,
        grouper=grouper,
        train_kws=train_kws,
        adjust_kws=adjust_kws,
        qa=qa,
        seed=seed,
        multivariate=multivariate,
    )
    timing["preprocess"] = round(monotonic() - t0, 6)

    if control is not None:
        control.check_cancelled()

    t0 = monotonic()
    trained, trained_ds = _train(method, ref_t, hist_t, train_kws, seed=seed)
    timing["train"] = round(monotonic() - t0, 6)

    if control is not None:
        control.check_cancelled()

    t0 = monotonic()
    scen_t, sim_t = _adjust(
        method, trained, ref_t, hist_t, sim_t, adjust_kws, zeros, seed=seed
    )
    timing["adjust"] = round(monotonic() - t0, 6)

    t0 = monotonic()
    scen, clip_counts, post_trained = _finalize(
        scen_t,
        plan=plan,
        ref_t=ref_t,
        hist_t=hist_t,
        sim_t=sim_t,
        specs=specs,
        units=units,
        zeros=zeros,
        multivariate=multivariate,
        qa=qa,
    )
    timing["finalize"] = round(monotonic() - t0, 6)
    timing["total"] = round(monotonic() - t_start, 6)

    return CellResult(
        scen=scen,
        trained_ds=trained_ds,
        qa=qa,
        timing=timing,
        clip_counts=clip_counts,
        trained_obj=trained,
        zeros=zeros,
        post_trained=post_trained,
    )


def adjust_trained(
    plan: CellPlan,
    trained_obj: Any,
    *,
    sim: xr.Dataset,
    ref: xr.Dataset,
    hist: xr.Dataset,
    specs: dict[str, VariableSpec],
    units: dict[str, str],
    zeros: ZeroHandling | None = None,
    post_trained: tuple[tuple[MethodSpec, Any, dict[str, Any]], ...] | None = None,
    qa: list[QACheck] | None = None,
) -> xr.Dataset:
    """Apply Steps 6-7 of the cell contract using an already-trained object.

    Intended for evaluation blocks that reuse a fitted ``trained_obj`` and
    ``ZeroHandling`` state without re-training or re-emitting sample-size QA.
    """
    if qa is None:
        qa = []
    method = _validate(plan, specs, qa)
    if method.family == "train_adjust" and trained_obj is None:
        raise ValueError(f"trained_obj is required for method '{method.name}'.")

    multivariate = method.min_vars > 1
    ref_t = _to_tensor(ref, plan.variables, multivariate=multivariate, units=units)
    hist_t = _to_tensor(hist, plan.variables, multivariate=multivariate, units=units)
    sim_t = _to_tensor(sim, plan.variables, multivariate=multivariate, units=units)

    _, adjust_kws, _ = _fill_registry_defaults(
        plan, method, specs, ref_t, qa, emit_sample_size_qa=False
    )
    scen_t, sim_t = _adjust(
        method, trained_obj, ref_t, hist_t, sim_t, adjust_kws, zeros
    )
    scen, _, _ = _finalize(
        scen_t,
        plan=plan,
        ref_t=ref_t,
        hist_t=hist_t,
        sim_t=sim_t,
        specs=specs,
        units=units,
        zeros=zeros,
        multivariate=multivariate,
        qa=qa,
        fitted_post=post_trained,
    )
    return scen
