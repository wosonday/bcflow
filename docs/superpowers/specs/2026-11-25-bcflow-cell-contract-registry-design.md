# bcflow Cell Contract & Registry Upgrade Design Specification

**Date:** 2026-11-25
**Status:** Proposed (Ready for Claude Opus verification)
**Companion Plan:** [2026-11-25-bcflow-cell-contract-registry-plan.md](../plans/2026-11-25-bcflow-cell-contract-registry-plan.md)
**Reference Source:** `D:\Project\GCMs-bias-correction` (`gcmbc`)

## 1. Context, Goals, Non-Goals, and Locked User Decisions

### 1.1. Context

`bcflow` already has strong production data engineering compared to `gcmbc`:

- Full gridded observation reference support (`grid:` in [config_schema.py](../../../src/bcflow/config_schema.py)) with 6 sparse-matrix regridding algorithms in [regrid.py](../../../src/bcflow/regrid.py), `block_size` chunking, `float32` compaction, and yearly NetCDF streaming in [writers.py](../../../src/bcflow/writers.py).
- Curvilinear 2D grid extraction via unit-sphere `KDTree` and `search_radius` fallback in [extract.py](../../../src/bcflow/extract.py).
- CPU/RAM/cgroup-aware `WorkerPool` (`spawn` process reuse, single-thread BLAS/OpenMP, SIGINT isolation) in [resources.py](../../../src/bcflow/resources.py).
- Cooperative cancellation (`RunControl` and `--cancel-file`), JSON-lines streaming in [cli.py](../../../src/bcflow/cli.py), seeded reproducibility (`_SEED = 0`), and a working local Streamlit UI in `app/`.

However, `bcflow`'s adjustment core in [adjust.py](../../../src/bcflow/adjust.py), [methods.py](../../../src/bcflow/methods.py), [variables.py](../../../src/bcflow/variables.py), and [grouping.py](../../../src/bcflow/grouping.py) has structural limitations compared to `gcmbc` (`gcmbc/registry/methods.py`, `gcmbc/registry/variables.py`, `gcmbc/adjust/runner_cell.py`, `gcmbc/adjust/preprocess.py`, `gcmbc/adjust/grouping.py`):

1. **Thin method registry:** `bcflow` only registers 4 univariate `TrainAdjust` classes with no metadata describing arity, family (`train_adjust` vs `adjust_only` vs `post`), projection capability, frequency-adaptation support, or optional extras.
2. **Method defaults mixed into `VariableSpec`:** [variables.py](../../../src/bcflow/variables.py) currently hardcodes `train_params={"nquantiles": 50}` and `adjust_params={"interp": "linear", "extrapolation": "constant"}` on every `VariableSpec`. Several `xsdba 0.7.0` methods (`Scaling`, `LOCI`, `PrincipalComponents`, etc.) reject these keywords.
3. **Silent clipping & coarse preprocessing guards:** `_clip()` in [adjust.py](../../../src/bcflow/adjust.py) clips silently without QA counts, `ZeroHandling.jitter()` is tied only to `jitter_under_thresh` rather than whether a method uses quantiles, and there is no guard against double `adapt_freq` or insufficient sample sizes per group.
4. **Univariate-only execution in `adjust.py`:** Training/adjustment logic is embedded directly inside `_correct()` instead of a clean, method-agnostic cell contract (`CellPlan -> run_cell() -> CellResult`).

### 1.2. Locked User Decisions

The following three architectural decisions are locked by the user and govern this entire design:

1. **Two-wave phased rollout:** Implement and stabilize **Phase 1 -> Phase 2** first (hardening the core registries, preprocessing, grouping QA, 7-step `CellContract`, and `adjust.py` / `writers.py` refactor with compact diffs). Only after Phase 1 -> 2 is verified and merged do we proceed to **Phase 3 -> Phase 4** (schema/UI expansion and multivariate `targets:`).
2. **Additive `bcflow methods --json` contract:** Preserve the existing 3 keys (`name`, `class`, `summary`) on every entry returned by `list_methods()` and **only add new metadata fields** (`family`, `min_vars`, `supports_projection`, `native_adapt_freq`, `uses_quantiles`, `extra`).
3. **Preserve `bcflow` pre-train `adapt_freq` semantics:** Keep `bcflow`'s existing seeded `xsdba.processing.adapt_freq` preprocessing step before `train()` to maintain numerical parity with existing `bcflow` outputs and tests, while adding a double-application guard via `MethodMeta.native_adapt_freq` when a user explicitly sets `adapt_freq_thresh` inside `train_params`.

### 1.3. Goals and Non-Goals

**Goals:**

- Separate dependency-free method discovery metadata (`src/bcflow/discovery.py`) from runtime `xsdba` binding (`src/bcflow/methods.py`) while cataloging all 11 `xsdba 0.7.0` adjustment classes.
- Enforce physical invariants on `VariableSpec` (`kind == "*"` implies `bounds[0] >= 0.0`) and decouple method-owned default parameters from variable-owned defaults, fixing latent parameter-mismatch bugs on `Scaling` and new methods.
- Extract grouping QA (`sample_size_qa`) and preprocessing (`src/bcflow/preprocess.py`: quantile-gated seeded jitter, double-`adapt_freq` guard, QA-counted `clip_to_bounds`).
- Introduce the 7-step `run_cell()` contract (`src/bcflow/cell.py`) supporting `train_adjust`, `adjust_only`, `post` chains, and `multivar` stacking, and refactor `adjust.py` / `writers.py` onto it without breaking `WorkerPool`, `compact=True`, `RunControl`, or numerical parity.
- Provide a clean forward design for Phase 3 (early semantic validation, `post:` chains, `loci`, `season`, `custom_variables:`, UI) and Phase 4 (multivariate `targets:` across both stations and grid blocks).

**Non-Goals (Deferred beyond Phase 1–4):**

- Content-addressed Zarr cell caching (`RunStore`), blocked k-fold cross-validation (`cv:`), 2-phase `evaluate -> produce` ranking, `gcmbc.writers` entry-point plugins, and multi-model / multi-scenario orchestration. The Phase 2 `CellPlan` / `CellResult` boundary is explicitly designed so these can wrap `run_cell()` later without rewriting the math core.

---

## 2. Verified Baseline Invariants & Empirical `xsdba 0.7.0` Audit

### 2.1. Existing `bcflow` Invariants (Must Not Break)

1. **Stdlib-only discovery ([test_discovery.py](../../../tests/test_discovery.py)):**
   `StdlibOnly` injects a `sys.meta_path` hook blocking every non-stdlib module except `bcflow` during `import bcflow`, `bcflow --version`, `bcflow methods`, and `bcflow methods --json`. Therefore `src/bcflow/discovery.py` and [src/bcflow/__init__.py](../../../src/bcflow/__init__.py) **must never import `xsdba`, `numpy`, `xarray`, `pandas`, `pydantic`, or `yaml` at import time**.
2. **Per-variable failure isolation & multi-error `validate` ([test_pipeline_e2e.py](../../../tests/test_pipeline_e2e.py), [test_writers.py](../../../tests/test_writers.py), [test_service_cli.py](../../../tests/test_service_cli.py)):**
   - `test_unknown_method_fails_only_that_variable` and `test_failed_variable_is_listed_in_manifest_and_others_still_written` pass an unknown `method` inside `correction.<var>.method` and expect `load_config()` to succeed, `run_pipeline()` to record a `fail` `QACheck` (`method:<var>:unknown`) for that variable only, and all other variables to complete normally.
   - `test_validate_reports_every_missing_input_and_bad_method` passes a config containing simultaneously an unknown `method`, missing `model.sim`, and broken `stations.file_pattern`, asserting that `validate_config_file()` in [service.py](../../../src/bcflow/service.py) reports all three errors in one pass. Because `validate_config_file()` only runs `_semantic_errors(cfg)` after `load_config(raw)` succeeds, method validation belongs in `_semantic_errors()` (for `bcflow validate`) and `run_cell()` / `_check_method()` (for per-variable isolation at runtime), **not** inside `PipelineConfig` for single-variable `correction.<var>.method` in Phase 1–2.
3. **`group="season"` rejection tests ([test_config_schema.py](../../../tests/test_config_schema.py), [test_service_cli.py](../../../tests/test_service_cli.py)):**
   Both `test_invalid_correction_rejected` (`{"group": "season"}`) and `test_validate_reports_schema_errors_with_their_location` explicitly test that `group: "season"` is rejected by `CorrectionConfig`. Keep `GROUPS = ("dayofyear", "month", "none")` in Phase 1–2; enable `"season"` in Phase 3 (updating those two negative fixtures to `"week"`, which is unsupported on CMIP6 `CFTimeIndex`).
4. **Latent Bug Fixed by Method-Owned Defaults ([variables.py](../../../src/bcflow/variables.py)):**
   Currently `VariableSpec` defines `train_params={"nquantiles": 50}` and `adjust_params={"interp": "linear", "extrapolation": "constant"}`, and [adjust.py](../../../src/bcflow/adjust.py) passes `**spec.train_params` and `**spec.adjust_params` unconditionally. On `xsdba 0.7.0`, `Scaling._train` rejects `nquantiles`, `Scaling._adjust` and `LOCI._adjust` reject `extrapolation`, `LOCI._train` rejects `nquantiles` and `kind`, and `PrincipalComponents._adjust` rejects both `interp` and `extrapolation`. Moving default `train_params`/`adjust_params` onto `MethodMeta` resolves this bug cleanly while keeping `VariableSpec`'s `precip`-specific quantile override (`interp="nearest"`).
5. **Pickle/spawn safety & sequential-parallel parity ([adjust.py](../../../src/bcflow/adjust.py), [test_parallel_grid.py](../../../tests/test_parallel_grid.py)):**
   `_correct_block_task()` executes in spawned worker processes (`WorkerPool`). `xsdba`'s pickle state omits `.ds`, so workers must return `trained_ds: xr.Dataset | None` (never live `TrainAdjust` instances, open files, or callbacks). `test_two_process_grid_matches_sequential` requires `workers=2` to match `workers=1` closely across `scen`, `adj.ds`, `evaluation`, and `hist_eval`.
6. **Frontend `app/` compatibility ([app/ui/configure.py](../../../app/ui/configure.py), [app/ui/results.py](../../../app/ui/results.py)):**
   `app/ui/configure.py` feeds `list_methods()` directly into `st.dataframe()`, so adding metadata columns works automatically. `app/ui/results.py` skips `record.get("adj")` when `None`, so `"adj": null` in `manifest.json` (`schema_version: 1`) requires zero frontend changes in Phase 1–2.

### 2.2. Empirical `xsdba 0.7.0` Signature & Behavior Audit

Verified directly against `xsdba 0.7.0` installed in `.venv-windows` (`ot` / `POT` is not installed by default):

| `bcflow` name | `xsdba` class | `family` | `min_vars` | `supports_projection` | `native_adapt_freq` | `uses_quantiles` | `extra` | `registry_defaults` | Verified signature & runtime quirks |
|---|---|---|---|---|---|---|---|---|---|
| `empirical_quantile_mapping` | `EmpiricalQuantileMapping` | `train_adjust` | `1` | `true` | `true` | `true` | `null` | `("kind",)` | `_train(..., nquantiles=20, kind='+', group='time', adapt_freq_thresh=None)`; `_adjust(sim, interp='nearest', extrapolation='constant')` |
| `detrended_quantile_mapping` | `DetrendedQuantileMapping` | `train_adjust` | `1` | `true` | `true` | `true` | `null` | `("kind",)` | `_train(..., nquantiles=20, kind='+', group='time', adapt_freq_thresh=None)`; `_adjust(sim, interp='nearest', extrapolation='constant', detrend=1)` |
| `quantile_delta_mapping` | `QuantileDeltaMapping` | `train_adjust` | `1` | `true` | `true` | `true` | `null` | `("kind",)` | `_train(..., nquantiles=20, kind='+', group='time', adapt_freq_thresh=None)`; `_adjust(sim, interp='nearest', extrapolation='constant', rank_window=None)` |
| `scaling` | `Scaling` | `train_adjust` | `1` | `true` | `false` | `false` | `null` | `("kind",)` | `_train(ref, hist, *, group='time', kind='+')` (NO `nquantiles`); `_adjust(sim, interp='nearest')` (NO `extrapolation`) |
| `loci` | `LOCI` | `train_adjust` | `1` | `true` | `false` | `false` | `null` | `("thresh",)` | `_train(ref, hist, *, thresh, group='time')` (NO `kind`, NO `nquantiles`); `_adjust(sim, interp='linear')` (NO `extrapolation`) |
| `extreme_values` | `ExtremeValues` | `post` | `1` | `true` | `false` | `true` | `null` | `("cluster_thresh",)` | `_train(ref, hist, *, cluster_thresh, ref_params=None, q_thresh=0.95)` (NO `group`); `_adjust(sim, scen, *, frac=None, power=None, interp='linear', extrapolation='constant')` |
| `principal_components` | `PrincipalComponents` | `train_adjust` | `2` | `true` | `false` | `false` | `null` | `("crd_dim",)` | `_train(ref, hist, *, crd_dim, best_orientation='simple', group='time')`; `_adjust(sim)` (NO `interp`, NO `extrapolation`) |
| `mbcn` | `MBCn` | `train_adjust` | `2` | `true` | `false` | `false` | `null` | `("base_kws_vars",)` | `_train(ref, hist, *, base_kws=None, adj_kws=None, n_escore=-1, n_iter=20, pts_dim='multivar', rot_matrices=None)`; `_adjust(sim, ref, hist, *, base=QDM, base_kws_vars=None, ...)` |
| `npdf_transform` | `NpdfTransform` | `adjust_only` | `2` | `true` | `false` | `false` | `null` | `()` | `has_train=False`; `_adjust(ref, hist, sim, ..., pts_dim='multivar') -> xr.Dataset` (**returns `Dataset` with `scenh` and `scen`, NOT `DataArray`**) |
| `otc` | `OTC` | `adjust_only` | `2` | `false` | `true` | `false` | `"POT"` | `()` | `has_train=False`; `_adjust(ref, hist, ..., adapt_freq_thresh=None, group='time', pts_dim='multivar')` (NO `sim` argument; requires `ot`) |
| `dotc` | `dOTC` | `adjust_only` | `2` | `true` | `true` | `false` | `"POT"` | `()` | `has_train=False`; `_adjust(ref, hist, sim, ..., kind=None, adapt_freq_thresh=None, group='time', pts_dim='multivar')` (requires `ot`) |

Note also: `METHOD_METADATA` in [discovery.py](../../../src/bcflow/discovery.py) must keep `summary` equal to line 1 of `cls.__doc__.strip().splitlines()[0]` because `test_discovery_metadata_matches_real_registry` asserts exact equality. All 11 docstrings above were verified directly from `xsdba 0.7.0`.

---

## 3. Target Module Architecture & Dependency Boundaries

```text
Layer 0 (Stdlib Only — enforced by tests/test_discovery.py):
  discovery.py (MethodMeta, METHOD_CATALOG, list_methods)
  qa.py        (QACheck, Status, helpers)
  _version.py

Layer 1 (Light Config & Specs — no xsdba, no xarray):
  variables.py     (VariableSpec + physical invariant, DEFAULTS, resolve, resolve_method_params)
  config_schema.py (Pydantic v2 schema; imports discovery.py + variables.py only)
  config_loader.py

Layer 2 (Data Prep & Scientific Primitives — imports xarray / xsdba):
  methods.py       (MethodSpec, METHODS, METHOD_REGISTRY, get_method, check_available)
  grouping.py      (build_grouper, effective_samples, sample_size_qa, resolve_latlon_dims)
  preprocess.py    (ZeroHandling / maybe_jitter, resolve_adapt_freq, apply_adapt_freq, clip_to_bounds)
  loaders.py, extract.py, regrid.py, units.py, timeaxis.py, coverage.py

Layer 3 (Execution Contract & Orchestration):
  cell.py          (PostStep, CellPlan, CellResult, run_cell — 7-step method-agnostic contract)
  adjust.py        (station & grid-block I/O, WorkerPool, _evaluate_block, calls run_cell)
  evaluate.py, resources.py, pipeline.py, writers.py

Layer 4 (Interfaces):
  service.py, cli.py  -->  app/ (Streamlit UI; never imported by src/bcflow/)
```

---

## 4. Phase 1 Detailed Design (Core Stabilization: Registries, Grouping QA, Preprocessing)

### 4.1. `src/bcflow/discovery.py` (Stdlib-Only Method Catalog)

Replace the raw 3-tuple `METHOD_METADATA` with typed stdlib dataclasses while preserving `METHOD_METADATA` (or keeping it derived) if any caller imports it directly:

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal

MethodFamily = Literal["train_adjust", "adjust_only", "post"]

@dataclass(frozen=True)
class MethodMeta:
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
```

**Design choices:**

- `default_train_params` and `default_adjust_params` use immutable `(key, value)` pairs on the frozen dataclass (avoiding mutable dict defaults while staying hashable and pickle-safe).
  - For `empirical_quantile_mapping`, `detrended_quantile_mapping`, `quantile_delta_mapping`: `default_train_params=(("nquantiles", 50),)` and `default_adjust_params=(("interp", "linear"), ("extrapolation", "constant"))`.
  - For `scaling`: `default_train_params=()`, `default_adjust_params=(("interp", "nearest"),)`.
  - For `loci`: `default_train_params=()`, `default_adjust_params=(("interp", "linear"),)`.
  - For `extreme_values`: `default_train_params=()`, `default_adjust_params=(("interp", "linear"), ("extrapolation", "constant"))`.
  - For `principal_components`, `mbcn`, `npdf_transform`, `otc`, `dotc`: empty `default_train_params=()` and `default_adjust_params=()`.
- `list_methods()` returns all 11 entries, each preserving the 3 existing keys and adding the new metadata fields (per Locked Decision 2):
  ```python
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
  ```
- **CLI plain-text output (`cli.py`):** `bcflow methods` without `--json` continues printing `f"{m['name']:<30} {m['summary']}"` (already uses `m['name']` and `m['summary']`, so additive dict keys do not break it).
- **`tests/test_discovery.py` update:** Update `_EXPECTED_METHODS` in [test_discovery.py](../../../tests/test_discovery.py) to include the 11 methods and new metadata keys while keeping `test_discovery_is_stdlib_only` and `test_discovery_metadata_matches_real_registry` strictly enforced.

### 4.2. `src/bcflow/methods.py` (Runtime Binding & Extra Availability)

```python
@dataclass(frozen=True)
class MethodSpec:
    meta: MethodMeta
    cls: type
    # Convenience properties delegating to meta (name, family, min_vars, ...)
```

- `METHODS: dict[str, MethodSpec]` maps every `MethodMeta` in `METHOD_CATALOG` to `getattr(xsdba.adjustment, meta.class_name)`.
- Keep `METHOD_REGISTRY: dict[str, type] = {name: spec.cls for name, spec in METHODS.items()}` for backward compatibility.
- Add `get_method(name: str) -> MethodSpec` raising `ValueError` with sorted valid names when unknown.
- Add `check_available(spec: MethodSpec) -> None`: if `spec.extra == "POT"` and `importlib.util.find_spec("ot") is None`, raise `RuntimeError` (or a dedicated subclass of `ValueError`—check whether `pipeline.py` isolates `Exception`, which it does) with clear install guidance (`pip install POT` / `uv add POT`).

### 4.3. `src/bcflow/variables.py` (Physical Invariants & Method-Decoupled Defaults)

1. **Physical invariant in `VariableSpec.__post_init__`:**
   ```python
   def __post_init__(self) -> None:
       if self.kind not in ("+", "*"):
           raise ValueError(f"variable {self.name!r}: kind must be '+' or '*', got {self.kind!r}")
       if self.kind == "*" and (self.bounds[0] is None or self.bounds[0] < 0.0):
           raise ValueError(
               f"variable {self.name!r}: kind '*' requires a non-negative lower bound, "
               f"got {self.bounds[0]!r}"
           )
   ```
   Verify all 6 `DEFAULTS` satisfy this: `precip`, `wind`, `solar` all have `kind="*"` and `bounds=(0.0, None)`; `tmax`, `tmin`, `humidity` have `kind="+"`.
2. **Decouple method defaults from `VariableSpec`:**
   - Remove hardcoded `"nquantiles": 50` and `{"interp": "linear", "extrapolation": "constant"}` from `VariableSpec` defaults. Instead:
     - `train_params: dict[str, Any] = field(default_factory=dict)`
     - `adjust_params: dict[str, Any] = field(default_factory=dict)`
     - `precip` keeps `adjust_params={"interp": "nearest"}` (its variable-specific preference for quantile methods), OR keeps an optional `quantile_adjust_params` / `adjust_params` merged conditionally only for quantile methods (`meta.uses_quantiles`).
   - Add helper `resolve_method_params(spec: VariableSpec, meta: MethodMeta) -> tuple[dict[str, Any], dict[str, Any]]`:
     - Start `train = dict(meta.default_train_params)`, update with `spec.train_params`.
     - Start `adjust = dict(meta.default_adjust_params)`. Only merge `spec.adjust_params` if `meta.uses_quantiles` is True OR when the user explicitly supplied overrides via `correction:` (track explicit overrides vs `DEFAULTS`, or put `precip`'s `interp="nearest"` only when `meta.uses_quantiles` so `precip` + `scaling` or `precip` + `loci` does not inherit incompatible defaults from `DEFAULTS["precip"]`, while explicit `correction.precip.adjust_params` is always respected!).
   - **Important check:** Keep `resolve(name, overrides)` returning a `VariableSpec` whose `method` defaults to `"quantile_delta_mapping"` and `group` defaults to `"month"`, preserving all existing tests in [test_config_schema.py](../../../tests/test_config_schema.py).

### 4.4. `src/bcflow/grouping.py` (Sample-Size QA & `pool_dims`)

1. Extend `build_grouper(spec_or_group, *, window: int = 1, pool_dims: tuple[str, ...] = ()) -> Grouper` (keeping existing signature `build_grouper(spec: VariableSpec)` working seamlessly):
   - Support `pool_dims` mapped to `add_dims=list(pool_dims) or None`.
   - Keep `GROUPS = ("dayofyear", "month", "none")` in Phase 1–2 (per Invariant 3), while structuring `_GROUPS_PER_YEAR` so `"season": 4` can be enabled cleanly in Phase 3.
2. Add `effective_samples(group: str, window: int, n_years: int) -> int` and `sample_size_qa(group: str, window: int, n_years: int, nquantiles: int, *, variable: str) -> QACheck`:
   - `window > 1` with `group in ("month", "season")` -> `status="warn"`, `name=f"grouping:{variable}:window_units"`, explaining `window` is measured in days (time steps), not months/seasons.
   - `nquantiles > samples // 3` -> `status="warn"`, `name=f"grouping:{variable}:sample_size"`.
   - Otherwise -> `status="pass"`, `name=f"grouping:{variable}:sample_size"`.

### 4.5. `src/bcflow/preprocess.py` (Extracted, Quantile-Gated, QA-Instrumented)

Extract preprocessing out of [adjust.py](../../../src/bcflow/adjust.py) into a dedicated `src/bcflow/preprocess.py`:

1. **`ZeroHandling` / `maybe_jitter()`:**
   - Preserve `bcflow`'s deterministic `np.random.default_rng(_SEED)` (`_SEED = 0`) and `np.nextafter(0.0, 1.0)` lower bound so existing runs (`test_random_steps_are_reproducible`) remain bit-identical.
   - **Gate jitter by `meta.uses_quantiles`:** only jitter when `spec.jitter_under_thresh` is set **and** `meta.uses_quantiles` is True (e.g., `QDM`/`EQM`/`DQM` on `precip`, not `Scaling` or `LOCI`).
   - Emit `QACheck(name=f"preprocess:{variable}:jitter", status="pass", ...)` once per variable (or per block, deduplicated by `adjust.py` just like other pass checks on block 1).
2. **`resolve_adapt_freq()` & `apply_adapt_freq()` (Honoring Locked Decision 3):**
   - **Default behavior (Decision 3):** Keep `bcflow`'s existing seeded `np.random.seed(_SEED)` + `xsdba.processing.adapt_freq(ref_cal, hist_cal, thresh=thresh, group=group)` before `train()`. Emit `QACheck(name=f"adapt_freq:{variable}:applied", status="pass", ...)` (preserving the exact check name!).
   - **Double-application guard:** Before calling `xsdba.processing.adapt_freq(...)`, check whether `adapt_freq_thresh` was already explicitly supplied by the user inside `train_params`:
     - If `"adapt_freq_thresh"` is in `train_params` AND `meta.native_adapt_freq` is True: skip external `xsdba.processing.adapt_freq` (let `xsdba` apply it natively once) and record that native `adapt_freq_thresh` is used—**never apply twice**.
     - If `"adapt_freq_thresh"` is in `train_params` AND `meta.native_adapt_freq` is False: raise a clear `ValueError` (or strip & warn? Failing fast with a clear message is cleaner, and Phase 3 can catch it in `validate`).
   - **Documented caveat for Opus review:** Note that `bcflow` currently runs `adapt_freq` on `hist_cal` only (not `sim`), whereas `gcmbc` applies it natively or to both `hist` and `sim`. Keeping `hist_cal`-only pre-train `adapt_freq` satisfies Decision 3 (full numerical parity with existing `bcflow` tests).
3. **`clip_to_bounds()`:**
   - Replaces silent `_clip()` in `adjust.py`.
   - Counts `n_below` (`< low`) and `n_above` (`> high`) using finite mask (`(var < low)` / `(var > high)`), preserves `attrs`, and emits:
     - `QACheck(name=f"bounds:{variable}:clipped", status="warn", ...)` when `n_clipped > 0`.
     - `QACheck(name=f"bounds:{variable}:ok", status="pass", ...)` when bounds exist and `n_clipped == 0`.
   - **Note for block runs:** In `_run_grid()`, `block_qa` already filters out `status == "pass"` checks for blocks `> 1` ([adjust.py](../../../src/bcflow/adjust.py#L364-L368)). For `bounds:<var>:clipped` (`warn`), `_finish()` should either aggregate clip counts across blocks or let per-block warnings be deduplicated/summarized cleanly so a 200-block grid does not spam 200 clip warnings into `qa.json`. (Design choice: return clip counts in `CellResult` or aggregate in `_finish()`, emitting one summary `bounds:<var>:clipped` or `bounds:<var>:ok` check per variable!)

---

## 5. Phase 2 Detailed Design (7-Step Cell Contract & `adjust.py`/`writers.py` Refactor)

### 5.1. `src/bcflow/cell.py` — Unified 7-Step Contract

Define pickle-safe dataclasses (no closures, no open datasets, no `TrainAdjust` across process boundaries):

```python
@dataclass(frozen=True)
class PostStep:
    method: str
    train_params: tuple[tuple[str, Any], ...] = ()
    adjust_params: tuple[tuple[str, Any], ...] = ()

@dataclass(frozen=True)
class CellPlan:
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
    scen: xr.Dataset
    trained_ds: xr.Dataset | None
    qa: list[QACheck]
    timing: dict[str, float]
    trained_obj: Any | None = None  # Stripped to None before returning from WorkerPool
```

**Note on `trained_obj`:** In-process callers (`adjust.py` single-worker evaluation or `_evaluate_block`) can use `trained_obj` (the live `TrainAdjust` instance) for fast `calibration`/`validation` evaluation without reconstructing from `trained_ds`, while `_correct_block_task()` sets `trained_obj = None` (keeping only `trained_ds`) before sending `CellResult` back across `ProcessPoolExecutor`! Wait: in `_run_grid()`, `_evaluate_block()` runs *inside* the worker (`_correct()`) before returning to the parent, so `trained_obj` never even needs to cross the process boundary—`CellResult` can return `trained_ds` cleanly and `_correct()` only keeps `trained_obj` transiently if needed, or `run_cell()` does not even expose `trained_obj` outside `cell.py` if `_evaluate_block` uses `trained_ds` or a lightweight helper! Even simpler: let `CellResult` return `trained_obj: Any | None = None`, and `_correct_block_task()` strips `part["adj"] = _CombinedAdjustment(part["adj"].ds)` just as today.

**Exact 7 steps inside `run_cell(plan, *, ref, hist, sim, specs, control=None)`:**
Inputs `ref`, `hist`, `sim` are `xr.Dataset` objects aligned on daily time axis and harmonized units (already sliced to the calibration period for `ref` and `hist`, and simulation period for `sim`):

1. **Validate method availability & projection capability:**
   - Look up `method_spec = get_method(plan.method)` and call `check_available(method_spec)`.
   - If `plan.has_projection and not method_spec.meta.supports_projection`: raise `ValueError` (and append `QACheck` `fail`) explaining why `otc` cannot project `sim`.
2. **Select target tensor (`DataArray` vs `multivar` `Dataset`):**
   - `multivariate = method_spec.meta.min_vars > 1` (or `len(plan.variables) > 1`).
   - If `multivariate`: stack `ref`, `hist`, `sim` with `xsdba.processing.stack_variables(ds[list(plan.variables)], dim="multivar")`.
   - Else: select single `DataArray` `ds[plan.variables[0]]`.
3. **Fill `registry_defaults` & build `Grouper` + `sample_size_qa`:**
   - Fill `kind`, `thresh`, `cluster_thresh`, `crd_dim` (when absent from `train_kws`) from primary `VariableSpec` / `"multivar"`.
   - For `mbcn`: populate `adjust_kws.setdefault("base_kws_vars", {v: {"kind": specs[v].kind} for v in plan.variables})`.
   - Build `grouper = build_grouper(plan.group, window=plan.window, pool_dims=plan.pool_dims)`.
   - If `"nquantiles"` in `train_kws`: compute number of calibration years and emit `sample_size_qa(...)`.
4. **Preprocess (univariate path; multivariate handles per-variable bounds at step 7):**
   - Seeded `ZeroHandling(spec, units)` -> `maybe_jitter()` on `ref` and `hist` (only when `meta.uses_quantiles`).
   - `resolve_adapt_freq()` + seeded `xsdba.processing.adapt_freq(...)` on `hist` (or pass `adapt_freq_thresh` natively if user explicitly set it on a `native_adapt_freq` method).
   - Attach `group=grouper` to `train_kws` (or `train_kws.setdefault("base_kws", {}).setdefault("group", grouper)` for `mbcn`; skip `group` for `extreme_values` since `ExtremeValues._train` takes no `group`).
5. **Train (`family == "train_adjust"`):**
   - `trained = method_spec.cls.train(ref_t, hist_t, **train_kws)`
   - `trained_ds = trained.ds`
   - For `adjust_only`: `trained = None`, `trained_ds = None`.
6. **Adjust (dispatched solely by `family` + `supports_projection` + `mbcn` signature):**
   - `train_adjust`:
     - Jitter `sim_t` if `ZeroHandling` is active.
     - If `plan.method == "mbcn"`: `scen_t = trained.adjust(sim_t, ref_t, hist_t, **adjust_kws)`
     - Else: `scen_t = trained.adjust(sim_t, **adjust_kws)`
   - `adjust_only`:
     - If `method_spec.meta.supports_projection`: `scen_t = method_spec.cls.adjust(ref_t, hist_t, sim_t, **adjust_kws)`
     - Else (`otc`): `scen_t = method_spec.cls.adjust(ref_t, hist_t, **adjust_kws)`
     - **CRITICAL `NpdfTransform` unwrapping:** `NpdfTransform._adjust` returns an `xr.Dataset` containing `scenh` and `scen` instead of an `xr.DataArray`. If `isinstance(scen_t, xr.Dataset)` and `"scen" in scen_t`: extract `scen_t = scen_t["scen"]`.
7. **Post chain, unstack, zero-restore, clip, and dimension normalization:**
   - Apply each `PostStep` in `plan.post` (`ExtremeValues.train(ref_t, hist_t, **post_train)` -> `.adjust(sim_t, scen_t, **post_adjust)`), appending `QACheck(name=f"post:{plan.target_name}:{step.method}", status="pass", ...)`.
   - Unstack (`unstack_variables(scen_t, dim="multivar")`) or wrap `.to_dataset(name=plan.variables[0])`.
   - Restore zeros via `ZeroHandling.restore()` per variable (when active).
   - Clip via `clip_to_bounds()` per variable (recording clip counts).
   - Normalize dims `.transpose("time", ...)` on every variable in `scen` and preserve `units` attr.

### 5.2. Refactoring `src/bcflow/adjust.py` and `src/bcflow/writers.py`

**In [adjust.py](../../../src/bcflow/adjust.py):**

- Keep `_check_method(spec, qa)` at the start of `run_variable()` so unknown methods still record `QACheck(name=f"method:{spec.name}:unknown", status="fail", ...)` before loading any NetCDF/CSV files (preserving `test_unknown_method_fails_only_that_variable`).
- Replace raw `xsdba` training/adjusting inside `_correct()` with `CellPlan` construction + `run_cell()`.
- Keep `_correct()` signature and return dict (`scen`, `adj`, `tables`, `series`, `non_finite`, `units`, `calibration`) intact so `run_variable()`, `_run_grid()`, `_correct_block_task()`, and `_finish()` require minimal changes.
- Handle `adj` when `trained_ds is None` (`adjust_only` family):
  - In `_finish()`: if `parts[0]["adj"] is None`, `adj = None` (do not call `xr.concat([p["adj"].ds ...])`).
  - In `_evaluate_block()`: for `train_adjust`, evaluate `calibration` and `validation` using the trained `adj` object (same fast path as today); for `adjust_only` with `supports_projection=True`, evaluate via `run_cell()` per period; for `adjust_only` with `supports_projection=False` (`otc`), skip `validation` evaluation with a `warn` `QACheck`.
- Aggregate `bounds:<var>:clipped` across grid blocks in `_finish()` so a multi-block grid produces 1 clear summary `QACheck` per variable instead of N duplicate block warnings.
- Update `test_parallel_grid.py` / `WorkerPool` checks: verify `trained_ds` loads cleanly in `_correct()` when `trained_ds is not None`.

**In [writers.py](../../../src/bcflow/writers.py):**

- Update `write_result(..., adj, ...)` so when `adj is None`:
  - Do not write `<var>/adj.nc`.
  - Return `"adj": None` in the result dictionary.
- `write_manifest()` already runs `rel(written["adj"])` which returns `None` when `written["adj"]` is `None`, yielding `"adj": null` in `manifest.json` (`schema_version: 1`).
- [app/ui/results.py](../../../app/ui/results.py) already checks `elif value is not None:` before appending artifacts, so `app/` remains fully compatible.

### 5.3. QA Check Naming & Compatibility Table

| QA Check Name | Status | Phase Introduced | Semantics |
|---|---|---|---|
| `method:<var>:unknown` | `fail` | Existing (kept) | Unregistered method in `correction.<var>.method` |
| `adapt_freq:<var>:applied` | `pass` | Existing (kept) | Pre-train or native `adapt_freq` executed |
| `train_adjust:<var>:done` | `pass` | Existing (kept) | Summary check at end of `_finish()` |
| `adjust:<var>:non_finite` | `warn` | Existing (kept) | Valid model values turned NaN/inf |
| `grouping:<var>:sample_size` | `pass` / `warn` | Phase 1 (new) | Checks `samples_per_group >= 3 * nquantiles` |
| `grouping:<var>:window_units` | `warn` | Phase 1 (new) | `window > 1` on `month` / `season` |
| `preprocess:<var>:jitter` | `pass` | Phase 1 (new) | Seeded zero-jitter applied (quantile methods only) |
| `bounds:<var>:clipped` / `ok` | `warn` / `pass` | Phase 1–2 (replaces silent `_clip`) | Reports exact count of clipped values `[low, high]` |
| `post:<target>:<method>` | `pass` | Phase 2 (new) | Post-processor applied (e.g. `extreme_values`) |

---

## 6. Follow-On Design — Phase 3 & Phase 4 (Post-Stabilization Roadmap)

### 6.1. Phase 3 — Schema/Semantic Validation, `post:`, `loci`, `season`, Custom Variables & UI

Once Phase 1–2 is merged and verified:

1. **Extend `config_schema.py` (still importing only `discovery.py`, zero `xsdba`):**
   - Add `PostConfig(method, train_params, adjust_params)` and `post: list[PostConfig] = []`, `pool_dims: list[str] = []` to `CorrectionConfig`.
   - Enable `group="season"` (updating the 2 negative tests in `test_config_schema.py` and `test_service_cli.py` from `"season"` to `"week"`, with a clear `CFTimeIndex` `isocalendar()` error message).
   - Reject `window > 1` when `group == "none"`.
   - Reject `family == "post"` in `correction.<var>.method`, `family != "post"` in `post:`, `train_params` on `adjust_only` methods, and `min_vars > 1` on univariate `correction.<var>.method`.
   - Keep unknown method names checked in `service.py:_semantic_errors()` (or an opt-in strict mode) so `test_unknown_method_fails_only_that_variable` and multi-error `bcflow validate` keep passing.
   - Add optional `custom_variables: dict[str, CustomVariableConfig]` for variables outside the 6 built-ins (requiring explicit `units` and `kind`, plus `bounds[0] >= 0` when `kind == "*"`).
2. **Extend `service.py:_semantic_errors()`:**
   - Call `check_available(get_method(method))` for `method` and every `post` step so missing optional packages (e.g. `POT` for `otc`/`dotc`) fail cleanly during `bcflow validate` with install instructions.
3. **Update `app/ui/configure.py`:**
   - The `Available correction methods` expander already renders `list_methods()` via `st.dataframe()`; refine column order/labels (`name`, `family`, `min_vars`, `supports_projection`, `extra`, `summary`) and add guidance on `post:` and optional `POT`.

### 6.2. Phase 4 — Multivariate `targets:` on Both Stations & Grid Blocks

1. **Optional `targets:` section in `config_schema.py`:**
   - If `targets:` is omitted, `PipelineConfig` automatically derives one univariate target per entry in `variables:` (100% backward compatibility).
   - Each `TargetConfig(name, variables, method, group, window, pool_dims, train_params, adjust_params, post)` validates:
     - Every variable in `target.variables` is declared in `variables:` and mapped in `model.var_map` (and `grid.var_map` when `grid:` is used).
     - `len(target.variables) >= method_meta.min_vars`.
     - `mbcn` enforces `group == "none"` (`"time"`), because `MBCn` in `xsdba` does not support grouped seasonal/monthly runs in the same way as univariate QDM.
     - `otc` (`supports_projection == False`) is rejected when `sim:` is configured as a future projection (or guarded in `run_cell`).
2. **Multivariate execution in `adjust.py` & `pipeline.py`:**
   - **Station mode:** load all variables of the target, intersect station IDs (`xr.align(..., join="inner")` on `station` and `time`), harmonize units per variable, assemble `xr.Dataset`, and execute `run_cell()`.
   - **Grid mode:** scan all variables of the target to build the intersection `has_data` mask (`TargetGrid`), regrid each variable block using **that variable's own `spec.regrid`** (e.g., `precip` stays `conservative` while `tmax` stays `bilinear`), assemble block `xr.Dataset`, and execute `run_cell()` inside `WorkerPool`.
   - **Resource estimation in [resources.py](../../../src/bcflow/resources.py):** scale `estimate_task_bytes()` by `len(target.variables)` (plus a multiplier for iterative `mbcn`/`npdf_transform`) so `select_workers()` never over-allocates memory.
   - **Unpacking & writing:** split `CellResult.scen` back into per-variable outputs for `evaluate()` and `write_run()`, isolating failures per target in `pipeline.py`.

### 6.3. Phase 5+ Bridge (Out of Scope Here)

Once `CellPlan` (frozen) and `CellResult` are established, future work can add `gcmbc`-style content-addressed Zarr cell caching (`RunStore`), whole-year blocked k-fold cross-validation (`cv:`), and 2-phase `evaluate -> produce` ranking as an outer orchestration layer around `run_cell()` without altering the cell execution contract.

---

## 7. Opus Verification Checklist & Open Design Questions

Claude Opus should explicitly verify and challenge these 7 points before implementation starts:

1. **Per-variable `adjust_params` vs `MethodMeta` defaults:** Is gating `DEFAULTS["precip"].adjust_params = {"interp": "nearest"}` by `meta.uses_quantiles` (while always honoring explicit user `correction.<var>.adjust_params`) the cleanest way to avoid passing `interp="nearest"` when a user selects `scaling` or `loci` on `precip`?
2. **Locked Decision 3 (`adapt_freq`) edge case:** `bcflow` currently runs `xsdba.processing.adapt_freq(ref_cal, hist_cal, ...)` on `hist_cal` only, leaving `sim` untouched until `adj.adjust(sim)`. Should `preprocess.py` preserve `hist_cal`-only pre-train `adapt_freq` strictly for Phase 1–2 numerical parity, or also adapt `sim` for non-native methods like `scaling`/`loci` (or add a QA note)?
3. **Exposing all 11 methods in `list_methods()` during Phase 1:** In Phase 1–2, `methods --json` lists all 11 methods (including multivariate/post/POT ones) with `min_vars`, `family`, and `extra`, even though `targets:` (Phase 4) and `post:` config syntax (Phase 3) arrive in the second wave. Should `list_methods()` expose all 11 immediately, or should a `ready_for_config` / `phase` flag or doc note clarify which require Phase 3/4 config syntax?
4. **Error-reporting boundary (`load_config` vs `_semantic_errors`):** Keeping unknown `correction.<var>.method` out of `PipelineConfig` preserves `test_unknown_method_fails_only_that_variable` and `test_validate_reports_every_missing_input_and_bad_method`. Should Phase 3 follow the exact same pattern for `check_available()` (`POT`) and `post:` family checks (putting runtime/registry checks in `_semantic_errors` + `run_cell`), or move structural `post:` checks into `PipelineConfig`?
5. **`ZeroHandling.restore()` vs `post:` chain ordering:** In `cell.py` Step 7, should `ExtremeValues` (`post:`) run *before* `ZeroHandling.restore()` and `clip_to_bounds()` (so extreme-value adjustments cannot re-introduce negative or sub-trace values), or *after* `ZeroHandling.restore()`? Note `ExtremeValues._adjust(sim, scen)` consumes `scen`; running `post:` before `restore()` and `clip_to_bounds()` guarantees physical bounds and dry-day zeros hold on the final output.
6. **`float32` compaction (`compact=True`) vs `clip_to_bounds()`:** Should `scen = scen.astype("float32")` happen *after* `clip_to_bounds()` (current `bcflow` order: clip in `float64`, then cast to `float32` in `_correct`) or *before*? Keeping clip before `float32` cast avoids precision-rounding boundary violations (e.g. `-1e-9` or `1.0000001`).
7. **`adjust_only` evaluation semantics (`_evaluate_block`):** For `train_adjust`, `_evaluate_block` fits once on `calibration` and adjusts `hist[calibration]` and `hist[validation]`. For `adjust_only` (`npdf_transform`, `dotc`), calling `run_cell()` on `hist[validation]` uses `(ref[cal], hist[cal], hist[val])` cleanly, whereas `otc` (`supports_projection=False`) cannot adjust `hist[validation]` out-of-sample. Confirm skipping `validation` evaluation with a `warn` `QACheck` for `otc` is the desired behavior.

### 7.1. Opus Decisions (2026-11-25, binding for Wave 1)

Verified against `src/bcflow/methods.py` (today's registry holds 4 methods: 3 quantile methods + `scaling`); revised after Neon review, `variables.resolve()` (an override **replaces** a whole dict) and `adjust._correct()`.

1. **Accepted, with one fix.** Gate the *per-variable default* `adjust_params` by `meta.uses_quantiles`; always pass explicit user `correction.<var>.adjust_params` through (an invalid key then fails only that variable, via xsdba). Fix: `PipelineConfig.spec()` loses the default-vs-explicit distinction, so Task 2.2 must also pass the resolved `adjust_kws` from `CellPlan` into `_evaluate_block()`/`_adjust()` (today `adjust.py:652,761` read `spec.adjust_params` directly), and must read `override.adjust_params` / `override.train_params` from `PipelineConfig.correction[var]` and pass them as `explicit_*` to `resolve_method_params`. Merge order: `meta defaults < spec defaults (quantile only) < explicit user`. Documented edge-case change: a user override now *merges onto* method defaults instead of replacing them (e.g. `train_params: {kind: ...}` keeps `nquantiles=50` instead of falling to the xsdba default 20). No existing test relies on the old behaviour; note it in the README changelog.
2. **Parity: `hist_cal` only, no gating.** Keep pre-train `adapt_freq` on `hist_cal` only (xsdba design: the trained mapping carries the frequency fix to `sim`), applied whenever `spec.adapt_freq` is true, for every method including `scaling` (already runnable, so gating would change numbers). Only the double-application guard of Locked Decision 3 is new. `QACheck.status` stays `pass|warn|fail` (`qa.py:15`; `cli._ICON` has no `info`).
3. **Expose all 11, no new flag.** `list_methods()` lists all 11 with `family`/`min_vars`/`extra` (Locked Decision 2). In Wave 1, `_check_method()` rejects, for that variable only, any method with `min_vars > 1` ("needs `targets:` (Phase 4)") or `family == "post"` ("only valid in `post:` (Phase 3)"), and `check_available()` failures. The UI badges in Task 3.3 make this visible; no `phase` field to remove later.
4. **Same pattern as today.** Pydantic `PostConfig` checks only shape (`method: str`, `train_params: dict`, `adjust_params: dict`, matching `PostStep` and `config_schema.py:152-153`). Family checks (`post:` step must be `family == "post"`), `min_vars`, and `check_available()` go in `_semantic_errors()` (validate, collecting all errors) and `_check_method()`/`run_cell()` (run, failing one variable). `_semantic_errors()` uses `discovery` for family/min_vars and calls `check_available()` lazily.
5. **`post:` → `restore()` → `clip_to_bounds()` → `transpose`.** Confirmed. `ExtremeValues` consumes the still-jittered `scen` consistently with `sim`, and the final zeros/bounds guarantees hold on the written output.
6. **Clip in float64, then cast to float32.** Keep today's order (`_correct` line ~495). 0.0 and 1.0 are exact in float32, so the cast cannot break a bound.
7. **Confirmed.** `otc`: evaluate `calibration` in-sample, skip `validation` with `QACheck(name="evaluation:<var>:validation:skipped", status="warn")` (existing convention, `adjust.py:769`). `npdf_transform`/`dotc`: `run_cell()` per period with `sim = hist[period]`, seeded with `_SEED` so `test_random_steps_are_reproducible` style parity holds.
