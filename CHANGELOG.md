# Changelog

All notable changes to bcflow are documented here, following Keep a Changelog.

## [Unreleased]

### Added

- Three univariate methods implemented in bcflow itself (`bcflow.native`),
  grouped and trained like the xsdba methods, bringing the catalog to 14:
  - `variance_scaling`: `kind="+"` corrects the group mean and standard
    deviation; `kind="*"` scales the mean only.
  - `parametric_quantile_mapping`: method-of-moments `dist="norm"` (default for
    `kind="+"`) or `dist="gamma"` (default for `kind="*"`, fitted on values above
    `thresh`; values at or below it map to zero).
  - `power_transformation`: Leander and Buishand (2007) `a * sim ** b` for
    precipitation (`kind="*"` only); `b` matches the wet-day coefficient of
    variation and `a` matches the mean.
- `MethodMeta.module`, so catalog entries can point at any module, not only
  `xsdba.adjustment`.
- Evaluation metrics, reported per station/cell as raw vs corrected:
  `annual_cycle_rmse` (12 monthly climatologies), `lag1_autocorr_diff`, and
  `ks_stat` (two-sample Kolmogorov-Smirnov distance). With a wet-day threshold,
  the ETCCDI extremes `rx1day_bias`, `r95p_bias` (above the observed wet-day
  95th percentile), `cdd_bias` and `cwd_bias`. Annual indices and the annual
  cycle need at least one complete calendar year of daily data, otherwise NaN.
- `bcflow.evaluate.change_signal()` compares the projected mean change (`+`) or
  ratio (`*`) of the raw and corrected simulations.
- Public module `bcflow.results` with bounded, traversal-safe readers for run
  manifests and declared artifacts (`load_manifest`, `list_manifests`,
  `artifact_path`, `read_qa`, `read_evaluation`, `declared_artifacts`,
  `finite_values`, `resolve_config_paths`).

### Changed

- Evaluation CSVs contain additional metric rows; consumers that assume a fixed
  set of metrics must accept the new names.
- `extreme_values` passes `frac=0.70` and `power=3` explicitly. These equal
  xsdba's current defaults, so results are unchanged and the FutureWarning is gone.
- The Streamlit app reads run results through `bcflow.results` instead of its
  own copies; `app.services` keeps re-exporting the same functions.
- Internal: single-variable runs (`run_variable`) now use the same execution path
  as `targets:`. QA check names, progress labels and parameter defaults are
  unchanged.

## [0.2.0] - 2026-11-25

### Added

- An 11-method catalog shared by discovery and execution. `bcflow methods --json`
  retains `name`, `class`, and `summary`, and adds `family`, `min_vars`,
  `supports_projection`, `native_adapt_freq`, `uses_quantiles`, and `extra`.
- Ordered `post:` steps, including `extreme_values` after the primary adjustment.
- Seasonal grouping (`group: season`, DJF/MAM/JJA/SON) and `pool_dims` for pooling
  additional dimensions in the correction groups.
- `custom_variables` with explicit units, additive/multiplicative kind, and
  physical bounds for variables beyond the six built-ins.
- Multivariate `targets:` on both stations and grids, including
  `principal_components`, `mbcn`, and `npdf_transform`.
- `bcflow validate` checks for method family, minimum variable count (`min_vars`),
  projection support, and optional POT availability for `otc`/`dotc`.
- UI method badges for family, multivariate requirements, projection support,
  and optional dependencies.
- QA checks `grouping:*:sample_size` and `grouping:*:window_units` for grouping
  diagnostics, and `bounds:*:clipped` / `bounds:*:ok` for physical-bound checks
  with clipping counts.

### Changed

- User `correction.<var>.train_params` and `adjust_params` now **merge onto method
  defaults** instead of replacing the entire dictionary. For example, overriding
  `train_params.kind` on a quantile mapping method keeps `nquantiles=50` rather
  than falling back to xsdba's default.
- Per-variable default adjustment parameters apply only to quantile methods;
  explicit user parameters still take precedence for every method.
- Adjust-only methods may record `"adj": null` in `manifest.json` and omit
  `adj.nc`; consumers must allow a missing trained adjustment artifact.
- Configuration variable names are validated centrally against the built-ins
  plus `custom_variables`, including correction, model, and grid mappings.

### Fixed

- Seeded rotations make `mbcn` and `npdf_transform` reproducible for identical
  inputs and settings.
- LOCI handles string station IDs by defaulting to nearest interpolation when
  needed, while preserving an explicit interpolation choice.
- QA fail checks emitted before a cell raises are retained in the run report.
