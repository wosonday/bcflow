# bcflow Cell Contract & Registry Upgrade Implementation Plan

**Date:** 2026-11-25
**Design Spec:** [2026-11-25-bcflow-cell-contract-registry-design.md](../specs/2026-11-25-bcflow-cell-contract-registry-design.md)
**Python Environment:** `.\.venv-windows\Scripts\python.exe` (do not use `uv run` on this checkout; `.venv` is a Linux venv with a locked `.gitignore`).

## Rollout Strategy & Diff Hygiene

Per the user's decision, execution is split into two distinct waves:

1. **Wave 1 (This immediate focus): Phase 1 -> Phase 2.** Stabilizes the core registries, grouping QA, preprocessing, the 7-step `run_cell()` contract, and the `adjust.py` / `writers.py` refactor while maintaining 100% backward compatibility with all existing configs, CLI JSON outputs, `WorkerPool` parallelism, and numerical results.
2. **Wave 2 (Follow-on after Wave 1 verification & merge): Phase 3 -> Phase 4.** Expands `config_schema.py`, `service.py`, `app/`, and `pipeline.py` to expose `post:` chains, `loci`, `season` grouping, `custom_variables:`, and multivariate `targets:` across both station and grid workflows.

---

## Wave 1 — Phase 1: Registries, Grouping QA, and Preprocessing

### Task 1.1: Upgrade `src/bcflow/discovery.py` (Stdlib-Only 11-Method Catalog)

- **Files:**
  - Modify: [src/bcflow/discovery.py](../../../src/bcflow/discovery.py)
  - Modify: [tests/test_discovery.py](../../../tests/test_discovery.py)
  - Modify: [tests/test_service_cli.py](../../../tests/test_service_cli.py) (only if any assertion checks exact dict keys; current check uses `{"empirical_quantile_mapping", "quantile_delta_mapping"} <= names`)
- **Changes:**
  1. Keep imports strictly Python standard library (`__future__`, `dataclasses`, `typing`).
  2. Define `MethodFamily = Literal["train_adjust", "adjust_only", "post"]` and `@dataclass(frozen=True) class MethodMeta` with fields: `name`, `class_name`, `summary`, `family`, `min_vars`, `required_train`, `required_adjust`, `supports_projection`, `native_adapt_freq`, `uses_quantiles`, `extra`, `registry_defaults`, `default_train_params`, `default_adjust_params`.
  3. Populate `METHOD_CATALOG: tuple[MethodMeta, ...]` for all 11 `xsdba 0.7.0` classes (`empirical_quantile_mapping`, `detrended_quantile_mapping`, `quantile_delta_mapping`, `scaling`, `loci`, `extreme_values`, `principal_components`, `mbcn`, `npdf_transform`, `otc`, `dotc`), keeping `summary` equal to line 1 of each `xsdba` class docstring.
  4. Derive `METHOD_METADATA` and `METHOD_META_BY_NAME` from `METHOD_CATALOG`; add `get_method_meta(name: str) -> MethodMeta`.
  5. Update `list_methods()` to preserve `"name"`, `"class"`, `"summary"` and add `"family"`, `"min_vars"`, `"supports_projection"`, `"native_adapt_freq"`, `"uses_quantiles"`, `"extra"`.
- **Tests & Verification:**
  - Update `_EXPECTED_METHODS` in [tests/test_discovery.py](../../../tests/test_discovery.py) with all 11 entries and additive keys; ensure `test_discovery_is_stdlib_only` and `test_discovery_metadata_matches_real_registry` pass.
  - Run: `.\.venv-windows\Scripts\python.exe -B -m pytest tests/test_discovery.py tests/test_service_cli.py -q`

### Task 1.2: Upgrade `src/bcflow/methods.py` (Runtime `MethodSpec` & `check_available`)

- **Files:**
  - Modify: [src/bcflow/methods.py](../../../src/bcflow/methods.py)
  - Create: `tests/test_methods_registry.py`
- **Changes:**
  1. Define `@dataclass(frozen=True) class MethodSpec` holding `meta: MethodMeta` and `cls: type` (plus read-only property delegates `name`, `family`, `min_vars`, `supports_projection`, `native_adapt_freq`, `uses_quantiles`, `extra`, `registry_defaults`, `default_train_params`, `default_adjust_params`).
  2. Build `METHODS: dict[str, MethodSpec]` from `METHOD_CATALOG` and preserve `METHOD_REGISTRY: dict[str, type]` for existing callers.
  3. Implement `get_method(name: str) -> MethodSpec` and `check_available(spec: MethodSpec) -> None` (checking `importlib.util.find_spec("ot")` when `spec.extra == "POT"`).
- **Tests & Verification:**
  - Test all 11 methods resolve to real `xsdba.adjustment` classes, `get_method("unknown")` raises `ValueError`, and `check_available` raises an actionable error when `ot` is missing (mock `importlib.util.find_spec`).
  - Run: `.\.venv-windows\Scripts\python.exe -B -m pytest tests/test_methods_registry.py tests/test_discovery.py -q`

### Task 1.3: Harden `src/bcflow/variables.py` (Physical Invariant & Method-Decoupled Defaults)

- **Files:**
  - Modify: [src/bcflow/variables.py](../../../src/bcflow/variables.py)
  - Modify: [tests/test_config_schema.py](../../../tests/test_config_schema.py)
- **Changes:**
  1. Add `__post_init__` to `VariableSpec` enforcing `kind in ("+", "*")` and `kind == "*" => bounds[0] is not None and bounds[0] >= 0.0`.
  2. Remove `nquantiles=50` and `{"interp": "linear", "extrapolation": "constant"}` from `VariableSpec` defaults (move to `MethodMeta` in Task 1.1). Keep `DEFAULTS["precip"].adjust_params = {"interp": "nearest"}` (and empty dicts for other variables).
  3. Add `resolve_method_params(spec: VariableSpec, meta: MethodMeta, *, explicit_adjust: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]`:
     - `train_params = {**dict(meta.default_train_params), **spec.train_params}`
     - `adjust_params = dict(meta.default_adjust_params)`
     - Merge `spec.adjust_params` only when `meta.uses_quantiles` is True, and always merge `explicit_adjust` if provided.
  4. **Pitfall check:** Verify `PipelineConfig.spec(variable)` in [config_schema.py](../../../src/bcflow/config_schema.py) still returns a `VariableSpec` compatible with all existing callers and tests.
- **Tests & Verification:**
  - Add tests verifying `VariableSpec(name="x", units="1", kind="*", bounds=(None, None))` raises `ValueError`, and `resolve_method_params` for `(precip, scaling)` omits `nquantiles` and `extrapolation` while `(precip, quantile_delta_mapping)` keeps `nquantiles=50`, `interp="nearest"`, `extrapolation="constant"`.
  - Run: `.\.venv-windows\Scripts\python.exe -B -m pytest tests/test_config_schema.py -q`

### Task 1.4: Enhance `src/bcflow/grouping.py` (`pool_dims` & `sample_size_qa`)

- **Files:**
  - Modify: [src/bcflow/grouping.py](../../../src/bcflow/grouping.py)
  - Create: `tests/test_grouping.py`
- **Changes:**
  1. Extend `build_grouper(spec: VariableSpec | str, *, window: int = 1, pool_dims: tuple[str, ...] = ()) -> Grouper` so existing `build_grouper(spec)` calls stay unchanged while `cell.py` can pass `group`, `window`, and `pool_dims` (`add_dims`).
  2. Implement `effective_samples(group: str, window: int, n_years: int) -> int` and `sample_size_qa(group: str, window: int, n_years: int, nquantiles: int, *, variable: str) -> QACheck` (emitting `grouping:<var>:window_units` warning when `window > 1` with `month`/`season`, and `grouping:<var>:sample_size` warning when `nquantiles > samples // 3`).
- **Tests & Verification:**
  - Test `month` with `window=3` triggers `window_units` warning, short calibration period triggers `sample_size` warning, `dayofyear` with `window=31` over 30 years passes, and `pool_dims=("station",)` populates `Grouper.add_dims`.
  - Run: `.\.venv-windows\Scripts\python.exe -B -m pytest tests/test_grouping.py -q`

### Task 1.5: Extract `src/bcflow/preprocess.py` (Quantile-Gated Seeded Jitter, `adapt_freq` Guard, QA-Counted Bounds Clipping)

- **Files:**
  - Create: `src/bcflow/preprocess.py`
  - Create: `tests/test_preprocess.py`
- **Changes:**
  1. Move `ZeroHandling` from [adjust.py](../../../src/bcflow/adjust.py) into `src/bcflow/preprocess.py` with `maybe_jitter(da, spec, method_meta, seed=0)` that only jitters when `spec.jitter_under_thresh` is set **and** `method_meta.uses_quantiles` is True, returning `(da, QACheck | None)`.
  2. Implement `resolve_adapt_freq(spec: VariableSpec, method_meta: MethodMeta, train_kws: dict[str, Any]) -> tuple[str | None, bool]`:
     - Honors Locked Decision 3: defaults to pre-train `apply_adapt_freq()` (`is_native=False`) when `spec.adapt_freq` is True and `"adapt_freq_thresh"` is not in `train_kws`.
     - If user explicitly passed `"adapt_freq_thresh"` in `train_kws`: if `method_meta.native_adapt_freq` is True, return `(thresh, True)` so external `adapt_freq` is **not** called twice; if False, raise `ValueError`.
  3. Implement `apply_adapt_freq(ref_cal, hist_cal, thresh, group, *, variable: str, seed: int = 0) -> tuple[xr.DataArray, QACheck]` keeping `np.random.seed(seed)` and `QACheck(name=f"adapt_freq:{variable}:applied", ...)`.
  4. Implement `clip_to_bounds(ds: xr.Dataset, specs: dict[str, VariableSpec]) -> tuple[xr.Dataset, list[QACheck], dict[str, tuple[int, int]]]` counting `(n_below, n_above)` per variable and emitting `bounds:<var>:clipped` (`warn`) or `bounds:<var>:ok` (`pass`).
- **Tests & Verification:**
  - Test deterministic seeded jitter, skipped jitter on `scaling`, double-`adapt_freq` prevention when `adapt_freq_thresh` is in `train_kws`, and `clip_to_bounds` exact counts + preserved `attrs`.
  - Run: `.\.venv-windows\Scripts\python.exe -B -m pytest tests/test_preprocess.py -q`

---

## Wave 1 — Phase 2: 7-Step Cell Contract & `adjust.py` / `writers.py` Refactor

### Task 2.1: Implement `src/bcflow/cell.py` (`CellPlan`, `CellResult`, `run_cell`)

- **Files:**
  - Create: `src/bcflow/cell.py`
  - Create: `tests/test_cell_contract.py`
- **Changes:**
  1. Define frozen `PostStep`, frozen `CellPlan`, and `CellResult` dataclasses (`trained_ds: xr.Dataset | None`, `trained_obj: Any | None = None`).
  2. Implement the 7-step `run_cell(plan, *, ref, hist, sim, specs, units, control=None) -> CellResult` strictly by `MethodMeta` (`family`, `min_vars`, `supports_projection`, `native_adapt_freq`, `uses_quantiles`, `registry_defaults`), never branching on raw method names except for `xsdba` API quirks (`mbcn` passing `(sim, ref, hist)` + `base_kws` + `base_kws_vars`; `npdf_transform` extracting `scen_t["scen"]` from its returned `xr.Dataset`).
  3. Ensure `scen` runs `post:` steps before `ZeroHandling.restore()`, `clip_to_bounds()`, and `.transpose("time", ...)`.
  4. Ensure `trained_ds` and `scen` are pickle-safe when `trained_obj=None`.
- **Tests & Verification:**
  - Unit tests covering `quantile_delta_mapping`, `scaling`, `loci`, `mbcn` (2-variable stacked), `npdf_transform` (2-variable `adjust_only`), `otc` (`has_projection=False` succeeds; `has_projection=True` fails cleanly), and `extreme_values` as `post:` step.
  - Verify `pickle.dumps(plan)` and `pickle.dumps(result_without_trained_obj)` round-trip cleanly.
  - Run: `.\.venv-windows\Scripts\python.exe -B -m pytest tests/test_cell_contract.py -q`

### Task 2.2: Refactor `src/bcflow/adjust.py` onto `run_cell()`

- **Files:**
  - Modify: [src/bcflow/adjust.py](../../../src/bcflow/adjust.py)
- **Changes:**
  1. Preserve `_check_method(spec, qa)` at the top of `run_variable()` (keeps `test_unknown_method_fails_only_that_variable` passing without loading data).
  2. In `_correct()`:
     - Convert `ref`, `hist`, `sim` units/time axis via `harmonize()` and `align_daily()`, slice `ref_cal` and `hist_cal`.
     - Build `CellPlan` (with explicit `adjust_params` vs `DEFAULTS` handled via `resolve_method_params`) and call `run_cell()`.
     - Run `_evaluate_block()` (reusing `trained_obj` for `train_adjust`; calling `run_cell()` per period for `adjust_only` when `supports_projection=True`; warning/skipping `validation` when `supports_projection=False`).
     - Apply `compact=True` (`float32`) **after** `clip_to_bounds()`, materialize with `.load()`, and check `control.check_cancelled()`.
  3. In `_correct_block_task()`: wrap `trained_ds` in `_CombinedAdjustment(trained_ds)` (or `None` when `trained_ds is None`) so no live `TrainAdjust` instance crosses `ProcessPoolExecutor`.
  4. In `_finish()`: handle `adj = None` for `adjust_only` methods and aggregate per-block clip warnings into one variable-level `bounds:<var>:clipped` / `bounds:<var>:ok` `QACheck`.
- **Tests & Verification:**
  - Run: `.\.venv-windows\Scripts\python.exe -B -m pytest tests/test_pipeline_e2e.py tests/test_parallel_grid.py tests/test_optimizations.py tests/test_evaluate.py -q`

### Task 2.3: Support `adj=None` in `src/bcflow/writers.py` & Verify Frontend Safety

- **Files:**
  - Modify: [src/bcflow/writers.py](../../../src/bcflow/writers.py)
  - Modify: [tests/test_writers.py](../../../tests/test_writers.py)
- **Changes:**
  1. Update `write_result()` to skip writing `adj.nc` and return `"adj": None` when `adj is None` (both station and grid paths).
  2. Confirm `write_manifest()` emits `"adj": null` and `app/ui/results.py` renders cleanly without attempting to read a missing `adj` artifact.
- **Tests & Verification:**
  - Add a test in [tests/test_writers.py](../../../tests/test_writers.py) writing a result with `adj=None`, asserting `adj.nc` does not exist and `manifest["variables"][var]["adj"] is None`.
  - Run: `.\.venv-windows\Scripts\python.exe -B -m pytest tests/test_writers.py tests/test_web_results.py tests/test_web_results_ui.py -q`

### Task 2.4: Wave 1 Numerical Parity & Full Regression Gate

Run the complete `bcflow` + `app/` + harness test suite before moving to Wave 2:

```powershell
.\.venv-windows\Scripts\python.exe -B -m pytest -q
```

Verify:

1. Zero regressions across all existing tests (`test_pipeline_e2e.py`, `test_parallel_grid.py`, `test_optimizations.py`, `test_discovery.py`, `test_config_schema.py`, `test_service_cli.py`, `test_writers.py`, `test_web_*.py`).
2. `test_random_steps_are_reproducible` and `test_two_process_grid_matches_sequential` pass with exact parity.
3. All new Wave 1 unit tests (`test_methods_registry.py`, `test_grouping.py`, `test_preprocess.py`, `test_cell_contract.py`) pass.

---

## Wave 2 — Phase 3 & Phase 4 Roadmap (Execute After Wave 1 Merge)

### Phase 3 Tasks (Schema, Semantic Validation, `post:`, `loci`, `season`, Custom Variables & UI)

- **Task 3.1 (`config_schema.py`):** Add `PostConfig`, `CorrectionConfig.post`, `CorrectionConfig.pool_dims`, enable `"season"` in `GROUPS` (updating `"season"` -> `"week"` in [tests/test_config_schema.py](../../../tests/test_config_schema.py) and [tests/test_service_cli.py](../../../tests/test_service_cli.py)), reject `window > 1` on `group="none"`, add `custom_variables:`, and validate `post:` / `adjust_only` / `min_vars` constraints using `discovery.py` only (no `xsdba` import).
- **Task 3.2 (`service.py`):** Call `check_available(get_method(...))` inside `_semantic_errors()` for `method` and every `post:` step so `bcflow validate` reports missing `POT` cleanly alongside path/method errors.
- **Task 3.3 (`app/ui/configure.py`):** Improve the `Available correction methods` display table with `family`, `min_vars`, `supports_projection`, and `extra` badges/columns.
- **Task 3.4 (Verification):** Run `.\.venv-windows\Scripts\python.exe -B -m pytest tests/test_config_schema.py tests/test_service_cli.py tests/test_web_*.py -q`.

### Phase 4 Tasks (Multivariate `targets:` on Stations & Grid Blocks)

- **Task 4.1 (`config_schema.py`):** Add optional `targets: list[TargetConfig]`, defaulting to one univariate target per entry in `variables:`; validate variable membership, `len(variables) >= min_vars`, and `mbcn` requiring `group="none"`.
- **Task 4.2 (`adjust.py` & `loaders.py`):** Add `run_target()` supporting multi-variable station intersection (`join="inner"` on `station` and `time`) and multi-variable grid shared-`TargetGrid` intersection while preserving each variable's own `spec.regrid` (e.g. `conservative` for `precip`, `bilinear` for `tmax`).
- **Task 4.3 (`resources.py`):** Update `estimate_task_bytes()` to scale by `len(target.variables)` (plus multivariate overhead factor) so `select_workers()` respects RAM limits on multi-variable blocks.
- **Task 4.4 (`pipeline.py` & `writers.py`):** Orchestrate per-target execution with error/cancellation isolation and unpack multi-variable `CellResult.scen` into per-variable evaluation and disk outputs.
- **Task 4.5 (End-to-End Multivariate Verification):** Run `tests/test_multivariate_targets.py` exercising `principal_components`, `mbcn`, and `npdf_transform` on toy stations and toy grids (`workers=1` and `workers=2`), plus full `pytest -q`.
