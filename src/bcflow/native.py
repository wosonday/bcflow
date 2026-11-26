"""Grouped univariate adjustments implemented by bcflow."""

from __future__ import annotations

import numpy as np
import xarray as xr
from scipy import optimize, stats
from xsdba.adjustment import TrainAdjust
from xsdba.base import Grouper, map_blocks, map_groups
from xsdba.units import convert_units_to
from xsdba.utils import broadcast


def _cv(values: np.ndarray, exponent: float) -> float:
    transformed = values**exponent
    return float(np.std(transformed) / np.mean(transformed))


def _parameters(ref: np.ndarray, hist: np.ndarray, method: str, kind: str,
                dist: str, thresh: float) -> tuple[float, float, float, float]:
    """Return four scalar parameters for one group and one spatial cell."""
    ref = np.asarray(ref, dtype=float).ravel()
    hist = np.asarray(hist, dtype=float).ravel()
    ref = ref[np.isfinite(ref)]
    hist = hist[np.isfinite(hist)]
    if not len(ref) or not len(hist):
        return (np.nan,) * 4

    if method == "variance_scaling":
        if kind == "*":
            mean_hist = np.mean(hist)
            ratio = np.mean(ref) / mean_hist if mean_hist else 1.0
            return float(ratio), 0.0, 0.0, 0.0
        sd_hist = np.std(hist)
        ratio = np.std(ref) / sd_hist if sd_hist else 1.0
        return float(np.mean(hist)), float(ratio), float(np.mean(ref)), 0.0

    if method == "parametric_quantile_mapping":
        if dist == "gamma":
            ref = ref[ref > thresh]
            hist = hist[hist > thresh]
            if not len(ref) or not len(hist):
                return (np.nan,) * 4
            mean_ref, mean_hist = np.mean(ref), np.mean(hist)
            var_ref, var_hist = np.var(ref), np.var(hist)
            if var_ref <= 0 or var_hist <= 0:
                return (np.nan,) * 4
            return (float(mean_hist**2 / var_hist), float(var_hist / mean_hist),
                    float(mean_ref**2 / var_ref), float(var_ref / mean_ref))
        return (float(np.mean(hist)), float(max(np.std(hist), 1e-12)),
                float(np.mean(ref)), float(max(np.std(ref), 1e-12)))

    wet_ref = ref[ref > 0]
    wet_hist = hist[hist > 0]
    if not len(wet_ref) or not len(wet_hist):
        return (np.nan,) * 4
    difference = lambda exponent: _cv(wet_hist, exponent) - _cv(wet_ref, 1.0)
    lower, upper = difference(0.05), difference(8.0)
    exponent = float(optimize.brentq(difference, 0.05, 8.0)) if lower * upper < 0 else 1.0
    transformed_mean = np.mean(hist**exponent)
    factor = np.mean(ref) / transformed_mean if transformed_mean else 1.0
    return float(factor), exponent, 0.0, 0.0


@map_groups(p1=[Grouper.PROP], p2=[Grouper.PROP],
            p3=[Grouper.PROP], p4=[Grouper.PROP])
def _grouped_train(ds: xr.Dataset, *, dim: str | list[str], method: str,
                   kind: str, dist: str, thresh: float) -> xr.Dataset:
    """Fit the selected method on xsdba's groups and pooled dimensions."""
    ref_dims = Grouper.filter_dim(ds.ref, dim)
    hist_dims = Grouper.filter_dim(ds.hist, dim)
    params = xr.apply_ufunc(
        _parameters, ds.ref, ds.hist,
        input_core_dims=[np.atleast_1d(ref_dims).tolist(), np.atleast_1d(hist_dims).tolist()],
        output_core_dims=[[], [], [], []],
        kwargs={"method": method, "kind": kind, "dist": dist, "thresh": thresh},
        vectorize=True, dask="parallelized", output_dtypes=[float] * 4,
    )
    return xr.Dataset({f"p{i}": value for i, value in enumerate(params, 1)})


def _map_values(sim: np.ndarray, p1: np.ndarray, p2: np.ndarray,
                p3: np.ndarray, p4: np.ndarray, *, method: str, kind: str,
                dist: str, thresh: float) -> np.ndarray:
    if method == "variance_scaling":
        return sim * p1 if kind == "*" else (sim - p1) * p2 + p3
    if method == "power_transformation":
        return p1 * np.maximum(sim, 0) ** p2
    if dist == "gamma":
        probability = stats.gamma.cdf(np.maximum(sim, 0), a=p1, scale=p2)
        probability = np.clip(probability, 1e-10, 1 - 1e-10)
        adjusted = stats.gamma.ppf(probability, a=p3, scale=p4)
        return np.where(sim <= thresh, 0.0, adjusted)
    probability = stats.norm.cdf(sim, loc=p1, scale=p2)
    probability = np.clip(probability, 1e-10, 1 - 1e-10)
    return stats.norm.ppf(probability, loc=p3, scale=p4)


@map_blocks(reduces=[Grouper.PROP], scen=[])
def _grouped_adjust(ds: xr.Dataset, *, group: Grouper, interp: str,
                    method: str, kind: str, dist: str, thresh: float) -> xr.Dataset:
    """Broadcast trained parameters and apply the selected mapping."""
    params = [broadcast(ds[f"p{i}"], ds.sim, group=group, interp=interp)
              for i in range(1, 5)]
    scen = xr.apply_ufunc(
        _map_values, ds.sim, *params,
        kwargs={"method": method, "kind": kind, "dist": dist, "thresh": thresh},
        dask="parallelized", output_dtypes=[float],
    )
    return scen.rename("scen").to_dataset()


class _NativeGrouped(TrainAdjust):
    _allow_diff_calendars = False
    _allow_diff_training_times = False
    _method: str

    def __getstate__(self):
        state = dict(self.data)
        if not getattr(self, "_encoding_dataset", False) and hasattr(self, "ds"):
            state["_native_ds"] = self.ds
        return state

    def __setstate__(self, state):
        state = dict(state)
        self.ds = state.pop("_native_ds") if "_native_ds" in state else None
        self.data = state

    def set_dataset(self, ds: xr.Dataset) -> None:
        self._encoding_dataset = True
        try:
            super().set_dataset(ds)
        finally:
            self._encoding_dataset = False

    @classmethod
    def _fit(cls, ref: xr.DataArray, hist: xr.DataArray, *, group: str | Grouper,
             kind: str, dist: str = "", thresh: float = 0.0):
        if kind not in ("+", "*"):
            raise ValueError("kind must be '+' or '*'.")
        ds = _grouped_train(xr.Dataset({"ref": ref, "hist": hist}), group=group,
                            method=cls._method, kind=kind, dist=dist, thresh=thresh)
        return ds, {"group": group, "kind": kind, "dist": dist, "thresh": thresh}

    def _adjust(self, sim: xr.DataArray, interp: str = "nearest") -> xr.DataArray:
        return _grouped_adjust(
            self.ds.assign(sim=sim), group=self.group, interp=interp,
            method=self._method, kind=self.kind, dist=self.dist, thresh=self.thresh,
        ).scen


class VarianceScaling(_NativeGrouped):
    """Variance Scaling bias-adjustment.

    Additive scaling corrects group mean and standard deviation. Multiplicative
    scaling corrects only the mean; variance scaling targets temperature-like data.
    """

    _method = "variance_scaling"

    @classmethod
    def _train(cls, ref: xr.DataArray, hist: xr.DataArray, *,
               group: str | Grouper = "time", kind: str = "+"):
        return cls._fit(ref, hist, group=group, kind=kind)


class ParametricQuantileMapping(_NativeGrouped):
    """Parametric Quantile Mapping bias-adjustment.

    Normal and gamma parameters use method of moments. Gamma fits wet values
    above ``thresh`` and maps values at or below it to zero.
    """

    _method = "parametric_quantile_mapping"

    @classmethod
    def _train(cls, ref: xr.DataArray, hist: xr.DataArray, *,
               group: str | Grouper = "time", kind: str = "+",
               dist: str | None = None, thresh: str | float = 0.0):
        dist = dist or ("norm" if kind == "+" else "gamma")
        if dist not in ("norm", "gamma"):
            raise ValueError("dist must be 'norm' or 'gamma'.")
        threshold = float(convert_units_to(thresh, ref)) if isinstance(thresh, str) else float(thresh)
        return cls._fit(ref, hist, group=group, kind=kind, dist=dist, thresh=threshold)


class PowerTransformation(_NativeGrouped):
    """Power Transformation bias-adjustment.

    Leander and Buishand (2007) precipitation transform: a * sim ** b. The
    exponent matches wet-day coefficients of variation; scaling matches mean.
    """

    _method = "power_transformation"

    @classmethod
    def _train(cls, ref: xr.DataArray, hist: xr.DataArray, *,
               group: str | Grouper = "time", kind: str = "*"):
        if kind != "*":
            raise ValueError("power_transformation requires kind='*' for precipitation.")
        return cls._fit(ref, hist, group=group, kind=kind)
