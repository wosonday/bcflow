# Changelog

All notable changes to bcflow are documented here, following Keep a Changelog.

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
