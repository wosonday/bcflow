"""Preprocessing and physical bounds primitives for bias correction.

Extracted from adjust.py so cell execution and pipeline orchestration share
deterministic zero-jittering, frequency adaptation guards, and QA-counted
bounds clipping.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import xarray as xr
import xsdba
from xsdba.base import Grouper
from xsdba.units import convert_units_to

from .discovery import MethodMeta
from .qa import QACheck
from .variables import VariableSpec

# Fixed seed for the random steps (jitter of exact zeros, adapt_freq), so
# two runs on the same data write identical files.
SEED = 0


class ZeroHandling:
    """Jitter exact zeros below spec.jitter_under_thresh before xsdba sees
    the data, and set the values below it back to 0 afterwards.

    The same as xsdba.processing.jitter_under_thresh, but with a seeded
    random generator so two runs on the same data give identical files.
    """

    def __init__(self, spec: VariableSpec, units: str, seed: int = SEED):
        self.thresh = (
            float(convert_units_to(spec.jitter_under_thresh, units))
            if spec.jitter_under_thresh
            else None
        )
        self.rng = np.random.default_rng(seed)

    def jitter(self, da: xr.DataArray) -> xr.DataArray:
        if self.thresh is None:
            return da
        noise = self.rng.uniform(np.nextafter(0.0, 1.0), self.thresh, size=da.shape)
        return da.where(~((da < self.thresh) & da.notnull()), noise)

    def restore(self, da: xr.DataArray) -> xr.DataArray:
        if self.thresh is None:
            return da
        return da.where(~(da < self.thresh), 0.0)


def maybe_jitter(
    da: xr.DataArray,
    spec: VariableSpec,
    method_meta: MethodMeta,
    units: str,
    seed: int = SEED,
) -> tuple[xr.DataArray, QACheck | None]:
    """Jitter sub-threshold values only when both spec and method require it."""
    if not spec.jitter_under_thresh or not method_meta.uses_quantiles:
        return da, None
    jittered = ZeroHandling(spec, units, seed=seed).jitter(da)
    check = QACheck(
        name=f"preprocess:{spec.name}:jitter",
        status="pass",
        message=(
            f"Applied seeded zero-jitter below '{spec.jitter_under_thresh}' "
            f"for '{spec.name}'."
        ),
        context={
            "variable": spec.name,
            "thresh": spec.jitter_under_thresh,
            "seed": seed,
        },
    )
    return jittered, check


def resolve_adapt_freq(
    spec: VariableSpec,
    method_meta: MethodMeta,
    train_kws: Mapping[str, Any],
) -> tuple[str | None, bool]:
    """Decide whether adapt_freq runs pre-train, natively in xsdba, or not at all.

    Returns ``(thresh, is_native)``:
    - If ``adapt_freq_thresh`` is explicitly present in ``train_kws``:
      returns ``(train_kws["adapt_freq_thresh"], True)`` when the method supports
      native frequency adaptation so external ``adapt_freq`` is never applied
      twice; otherwise raises ``ValueError``.
    - Else if ``spec.adapt_freq`` is true:
      returns ``(spec.adapt_freq_thresh, False)``, raising ``ValueError`` when
      ``spec.adapt_freq_thresh`` is missing.
    - Otherwise returns ``(None, False)``.
    """
    if "adapt_freq_thresh" in train_kws:
        if not method_meta.native_adapt_freq:
            raise ValueError(
                f"Method '{method_meta.name}' does not support native "
                "'adapt_freq_thresh' in train_params."
            )
        return train_kws["adapt_freq_thresh"], True

    if spec.adapt_freq:
        thresh = spec.adapt_freq_thresh
        if not thresh:
            raise ValueError(
                f"correction.{spec.name}: adapt_freq=true requires 'adapt_freq_thresh'."
            )
        return thresh, False

    return None, False


def apply_adapt_freq(
    ref_cal: xr.DataArray,
    hist_cal: xr.DataArray,
    thresh: str,
    group: Grouper | str,
    *,
    variable: str,
    seed: int = SEED,
) -> tuple[xr.DataArray, QACheck]:
    """Run seeded xsdba.processing.adapt_freq on calibration hist only."""
    # adapt_freq draws from NumPy's global generator; seed it for reproducibility.
    np.random.seed(seed)
    hist_adapted, _pth, _dP0 = xsdba.processing.adapt_freq(
        ref_cal, hist_cal, thresh=thresh, group=group
    )
    check = QACheck(
        name=f"adapt_freq:{variable}:applied",
        status="pass",
        message=f"Completed frequency adaptation for '{variable}' (thresh={thresh}).",
        context={"variable": variable},
    )
    return hist_adapted, check


def clip_to_bounds(
    ds: xr.Dataset,
    specs: Mapping[str, VariableSpec],
) -> tuple[xr.Dataset, list[QACheck], dict[str, tuple[int, int]]]:
    """Clip Dataset variables to their VariableSpec bounds and record QA counts."""
    out = ds.copy(deep=False)
    checks: list[QACheck] = []
    counts: dict[str, tuple[int, int]] = {}

    for raw_name in ds.data_vars:
        name = str(raw_name)
        spec = specs.get(name)
        if spec is None:
            continue
        low, high = spec.bounds
        if low is None and high is None:
            counts[name] = (0, 0)
            continue

        da = ds[raw_name]
        n_below = int((da < low).sum().item()) if low is not None else 0
        n_above = int((da > high).sum().item()) if high is not None else 0
        counts[name] = (n_below, n_above)
        n_total = n_below + n_above

        out[raw_name] = da.clip(min=low, max=high, keep_attrs=True)

        context = {
            "variable": name,
            "bounds": [low, high],
            "n_below": n_below,
            "n_above": n_above,
            "n_clipped": n_total,
        }
        if n_total > 0:
            checks.append(
                QACheck(
                    name=f"bounds:{name}:clipped",
                    status="warn",
                    message=(
                        f"Clipped {n_total} value(s) of '{name}' to bounds "
                        f"[{low}, {high}] ({n_below} below, {n_above} above)."
                    ),
                    context=context,
                )
            )
        else:
            checks.append(
                QACheck(
                    name=f"bounds:{name}:ok",
                    status="pass",
                    message=f"All values of '{name}' are within bounds [{low}, {high}].",
                    context=context,
                )
            )

    return out, checks, counts
