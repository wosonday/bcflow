"""Runtime method registry binding discovery metadata to xsdba classes."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any

import xsdba.adjustment

from .discovery import METHOD_CATALOG, MethodFamily, MethodMeta


@dataclass(frozen=True)
class MethodSpec:
    """Runtime method specification pairing static metadata with its xsdba class."""

    meta: MethodMeta
    cls: type

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def family(self) -> MethodFamily:
        return self.meta.family

    @property
    def min_vars(self) -> int:
        return self.meta.min_vars

    @property
    def supports_projection(self) -> bool:
        return self.meta.supports_projection

    @property
    def native_adapt_freq(self) -> bool:
        return self.meta.native_adapt_freq

    @property
    def uses_quantiles(self) -> bool:
        return self.meta.uses_quantiles

    @property
    def extra(self) -> str | None:
        return self.meta.extra

    @property
    def registry_defaults(self) -> tuple[str, ...]:
        return self.meta.registry_defaults

    @property
    def default_train_params(self) -> tuple[tuple[str, Any], ...]:
        return self.meta.default_train_params

    @property
    def default_adjust_params(self) -> tuple[tuple[str, Any], ...]:
        return self.meta.default_adjust_params


METHODS: dict[str, MethodSpec] = {
    meta.name: MethodSpec(meta=meta, cls=getattr(xsdba.adjustment, meta.class_name))
    for meta in METHOD_CATALOG
}

METHOD_REGISTRY: dict[str, type] = {name: spec.cls for name, spec in METHODS.items()}


def get_method(name: str) -> MethodSpec:
    """Return the runtime MethodSpec for `name` or raise ValueError."""
    if name not in METHODS:
        raise ValueError(
            f"Method '{name}' is not registered. Valid methods: {sorted(METHODS)}."
        )
    return METHODS[name]


def check_available(spec: MethodSpec) -> None:
    """Verify optional dependencies required by `spec` are installed."""
    if spec.extra == "POT" and importlib.util.find_spec("ot") is None:
        raise ValueError(
            f"Method '{spec.name}' requires the optional 'POT' package "
            "(import 'ot'). Install it with: pip install POT"
        )
