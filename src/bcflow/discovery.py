"""Dependency-free method metadata shared by discovery and the runtime registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

MethodFamily = Literal["train_adjust", "adjust_only", "post"]


@dataclass(frozen=True)
class MethodMeta:
    """Static metadata describing a registered xsdba adjustment method."""

    name: str
    class_name: str
    summary: str
    family: MethodFamily
    min_vars: int
    required_train: tuple[str, ...] = ()
    required_adjust: tuple[str, ...] = ()
    supports_projection: bool = True
    native_adapt_freq: bool = False
    uses_quantiles: bool = False
    extra: str | None = None
    registry_defaults: tuple[str, ...] = ()
    default_train_params: tuple[tuple[str, Any], ...] = ()
    default_adjust_params: tuple[tuple[str, Any], ...] = ()


# Keep summaries equal to the first lines of the corresponding xsdba docstrings;
# test_discovery checks them against the installed implementations.
#
# default_train_params / default_adjust_params policy:
# - Quantile mapping methods (EQM, DQM, QDM) preserve legacy VariableSpec defaults
#   (nquantiles=50, interp="linear", extrapolation="constant") for numerical parity.
# - All other methods use empty defaults () unless xsdba requires a parameter with
#   no default; required parameters below (LOCI.thresh, ExtremeValues.cluster_thresh,
#   PrincipalComponents.crd_dim) and MBCn.base_kws_vars are injected at runtime via
#   registry_defaults, while Scaling.interp ("nearest"), LOCI.interp ("linear"),
#   and ExtremeValues interp/extrapolation already match xsdba's own defaults.
METHOD_CATALOG: tuple[MethodMeta, ...] = (
    MethodMeta(
        name="empirical_quantile_mapping",
        class_name="EmpiricalQuantileMapping",
        summary="Empirical Quantile Mapping bias-adjustment.",
        family="train_adjust",
        min_vars=1,
        supports_projection=True,
        native_adapt_freq=True,
        uses_quantiles=True,
        registry_defaults=("kind",),
        default_train_params=(("nquantiles", 50),),
        default_adjust_params=(("interp", "linear"), ("extrapolation", "constant")),
    ),
    MethodMeta(
        name="detrended_quantile_mapping",  # Cannon et al. (2015)
        class_name="DetrendedQuantileMapping",
        summary="Detrended Quantile Mapping bias-adjustment.",
        family="train_adjust",
        min_vars=1,
        supports_projection=True,
        native_adapt_freq=True,
        uses_quantiles=True,
        registry_defaults=("kind",),
        default_train_params=(("nquantiles", 50),),
        default_adjust_params=(("interp", "linear"), ("extrapolation", "constant")),
    ),
    MethodMeta(
        name="quantile_delta_mapping",
        class_name="QuantileDeltaMapping",
        summary="Quantile Delta Mapping bias-adjustment.",
        family="train_adjust",
        min_vars=1,
        supports_projection=True,
        native_adapt_freq=True,
        uses_quantiles=True,
        registry_defaults=("kind",),
        default_train_params=(("nquantiles", 50),),
        default_adjust_params=(("interp", "linear"), ("extrapolation", "constant")),
    ),
    MethodMeta(
        name="scaling",
        class_name="Scaling",
        summary="Scaling bias-adjustment.",
        family="train_adjust",
        min_vars=1,
        supports_projection=True,
        native_adapt_freq=False,
        uses_quantiles=False,
        registry_defaults=("kind",),
        # Scaling._train only needs group/kind (supplied via registry_defaults);
        # Scaling._adjust defaults interp="nearest" and rejects extrapolation.
        default_train_params=(),
        default_adjust_params=(),
    ),
    MethodMeta(
        name="loci",
        class_name="LOCI",
        summary="Local Intensity Scaling (LOCI) bias-adjustment.",
        family="train_adjust",
        min_vars=1,
        required_train=("thresh",),
        supports_projection=True,
        native_adapt_freq=False,
        uses_quantiles=False,
        registry_defaults=("thresh",),
        # LOCI._train requires `thresh` (supplied via registry_defaults);
        # LOCI._adjust defaults interp="linear" and rejects extrapolation.
        default_train_params=(),
        default_adjust_params=(),
    ),
    MethodMeta(
        name="extreme_values",
        class_name="ExtremeValues",
        summary="Adjustment correction for extreme values.",
        family="post",
        min_vars=1,
        required_train=("cluster_thresh",),
        supports_projection=True,
        native_adapt_freq=False,
        uses_quantiles=True,
        registry_defaults=("cluster_thresh",),
        # ExtremeValues._train requires `cluster_thresh` (supplied via registry_defaults);
        # ExtremeValues._adjust defaults interp="linear", extrapolation="constant".
        default_train_params=(),
        default_adjust_params=(),
    ),
    MethodMeta(
        name="principal_components",
        class_name="PrincipalComponents",
        summary="Principal component adjustment.",
        family="train_adjust",
        min_vars=2,
        required_train=("crd_dim",),
        supports_projection=True,
        native_adapt_freq=False,
        uses_quantiles=False,
        registry_defaults=("crd_dim",),
        # PrincipalComponents._train requires `crd_dim` (supplied via registry_defaults);
        # PrincipalComponents._adjust takes no extra parameters.
        default_train_params=(),
        default_adjust_params=(),
    ),
    MethodMeta(
        name="mbcn",
        class_name="MBCn",
        summary=(
            "Multivariate bias correction function using the N-dimensional "
            "probability density function transform."
        ),
        family="train_adjust",
        min_vars=2,
        supports_projection=True,
        native_adapt_freq=False,
        uses_quantiles=False,
        registry_defaults=("base_kws_vars",),
        # MBCn._train and _adjust all have defaults; per-variable kinds are
        # injected into `base_kws_vars` via registry_defaults.
        default_train_params=(),
        default_adjust_params=(),
    ),
    MethodMeta(
        name="npdf_transform",
        class_name="NpdfTransform",
        summary="N-dimensional probability density function transform.",
        family="adjust_only",
        min_vars=2,
        supports_projection=True,
        native_adapt_freq=False,
        uses_quantiles=False,
        registry_defaults=(),
        # NpdfTransform has no _train; all _adjust kwargs are optional.
        default_train_params=(),
        default_adjust_params=(),
    ),
    MethodMeta(
        name="otc",
        class_name="OTC",
        summary="Optimal Transport Correction.",
        family="adjust_only",
        min_vars=2,
        supports_projection=False,
        native_adapt_freq=True,
        uses_quantiles=False,
        extra="POT",
        registry_defaults=(),
        # OTC has no _train; all _adjust kwargs are optional.
        default_train_params=(),
        default_adjust_params=(),
    ),
    MethodMeta(
        name="dotc",
        class_name="dOTC",
        summary="Dynamical Optimal Transport Correction.",
        family="adjust_only",
        min_vars=2,
        supports_projection=True,
        native_adapt_freq=True,
        uses_quantiles=False,
        extra="POT",
        registry_defaults=(),
        # dOTC has no _train; all _adjust kwargs are optional.
        default_train_params=(),
        default_adjust_params=(),
    ),
)

METHOD_META_BY_NAME: dict[str, MethodMeta] = {m.name: m for m in METHOD_CATALOG}

METHOD_METADATA: tuple[tuple[str, str, str], ...] = tuple(
    (m.name, m.class_name, m.summary) for m in METHOD_CATALOG
)


def get_method_meta(name: str) -> MethodMeta:
    """Return static metadata for a registered method or raise ValueError."""
    if name not in METHOD_META_BY_NAME:
        raise ValueError(
            f"Method '{name}' is not registered. Valid methods: {sorted(METHOD_META_BY_NAME)}."
        )
    return METHOD_META_BY_NAME[name]


def list_methods() -> list[dict[str, Any]]:
    """Correction methods a config may name in `method:`."""
    return [
        {
            "name": m.name,
            "class": m.class_name,
            "summary": m.summary,
            "family": m.family,
            "min_vars": m.min_vars,
            "supports_projection": m.supports_projection,
            "native_adapt_freq": m.native_adapt_freq,
            "uses_quantiles": m.uses_quantiles,
            "extra": m.extra,
        }
        for m in METHOD_CATALOG
    ]
