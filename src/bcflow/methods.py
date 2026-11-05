""" Methods name registry for bias-correction pipeline.
This is a separate module so that the registry can be imported by other modules
without importing the entire bias-correction pipeline (which would create a circular import).
"""

from __future__ import annotations

import xsdba

METHOD_REGISTRY = {
    "empirical_quantile_mapping": xsdba.EmpiricalQuantileMapping,
    "detrended_quantile_mapping": xsdba.DetrendedQuantileMapping,  # Cannon et al. (2015)
    "quantile_delta_mapping": xsdba.QuantileDeltaMapping,
    "scaling": xsdba.Scaling,
}