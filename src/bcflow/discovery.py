"""Dependency-free method metadata shared by discovery and the runtime registry."""

# Keep summaries equal to the first lines of the corresponding xsdba docstrings;
# test_discovery checks them against the installed implementations.
METHOD_METADATA = (
    (
        "empirical_quantile_mapping",
        "EmpiricalQuantileMapping",
        "Empirical Quantile Mapping bias-adjustment.",
    ),
    (
        "detrended_quantile_mapping",  # Cannon et al. (2015)
        "DetrendedQuantileMapping",
        "Detrended Quantile Mapping bias-adjustment.",
    ),
    (
        "quantile_delta_mapping",
        "QuantileDeltaMapping",
        "Quantile Delta Mapping bias-adjustment.",
    ),
    ("scaling", "Scaling", "Scaling bias-adjustment."),
)


def list_methods() -> list[dict]:
    """Correction methods a config may name in `method:`."""
    return [
        {"name": name, "class": class_name, "summary": summary}
        for name, class_name, summary in METHOD_METADATA
    ]
