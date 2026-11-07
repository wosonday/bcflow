"""Best-effort resource limits, RAM estimates and the worker process pool."""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from math import isfinite
from pathlib import Path, PurePosixPath
from typing import Literal

import dask.system
import psutil

# Per worker: one thread for numpy's BLAS/OpenMP, so N workers do not each
# start N threads and fight over the same cores.
_SINGLE_THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")


@dataclass(frozen=True)
class WorkerSelection:
    workers: int
    cpu_limit: int
    memory_limit: int
    available_bytes: int
    budget_bytes: int
    estimated_task_bytes: int
    over_budget: bool


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError, UnicodeError:
        return None


def _cgroup_headrooms() -> list[int]:
    """Inspect conventional Linux v2/v1 mounts, including membership ancestors.

    Nonstandard mounts and inaccessible files are deliberately best effort.
    """
    root = Path("/sys/fs/cgroup")
    locations = {(root, True), (root / "memory", False)}
    membership = _read_text(Path("/proc/self/cgroup"))
    if membership:
        for line in membership.splitlines():
            fields = line.split(":", 2)
            if len(fields) != 3:
                continue
            _, controllers, name = fields
            if controllers == "":
                base, v2 = root, True
            elif "memory" in controllers.split(","):
                base, v2 = root / "memory", False
            else:
                continue
            relative = PurePosixPath(name)
            # Names outside the visible cgroup namespace cannot be resolved here.
            if not relative.is_absolute() or ".." in relative.parts:
                continue
            path = base.joinpath(*relative.parts[1:])
            while True:
                locations.add((path, v2))
                if path == base:
                    break
                path = path.parent

    headrooms = []
    for path, v2 in locations:
        limit_name = "memory.max" if v2 else "memory.limit_in_bytes"
        usage_name = "memory.current" if v2 else "memory.usage_in_bytes"
        limit = _read_text(path / limit_name)
        usage = _read_text(path / usage_name)
        if limit is None or usage is None:
            continue
        try:
            ceiling, used = int(limit), int(usage)
        except TypeError, ValueError:
            continue
        if ceiling >= 0 and used >= 0:
            used -= _reclaimable_cache(path, v2)
            headrooms.append(max(0, ceiling - max(0, used)))
    return headrooms


def _reclaimable_cache(path: Path, v2: bool) -> int:
    """Inactive file cache counted in the cgroup's usage.

    The kernel frees it on demand, so it is not used memory; container
    runtimes subtract it the same way ("working set" = usage - inactive_file).
    """
    stat = _read_text(path / "memory.stat")
    key = "inactive_file" if v2 else "total_inactive_file"
    for line in (stat or "").splitlines():
        name, _, value = line.partition(" ")
        if name == key:
            try:
                return max(0, int(value))
            except ValueError:
                return 0
    return 0


def _available_bytes() -> int:
    try:
        available = max(0, int(psutil.virtual_memory().available))
    except OSError, RuntimeError, AttributeError, TypeError, ValueError:
        return 0
    return min([available, *_cgroup_headrooms()])


def select_workers(
    requested: int | Literal["auto"],
    memory_fraction: float,
    remaining_blocks: int,
    estimated_task_bytes: int,
) -> WorkerSelection:
    """Cap concurrency by CPU, RAM, remaining work, and an optional request.

    Reserve one affinity/quota-aware CPU for the parent process and the
    system, but allow at least one worker when work remains. Unknown RAM also permits only one worker. An over-budget
    single task is reported rather than rejected; this is not an OOM guarantee.
    No remaining work returns zero workers.
    """
    if requested != "auto" and (
        not isinstance(requested, int) or isinstance(requested, bool) or requested < 1
    ):
        raise ValueError("requested must be 'auto' or a positive integer")
    if not isfinite(memory_fraction) or not 0 < memory_fraction <= 1:
        raise ValueError("memory_fraction must be finite and in (0, 1]")
    if remaining_blocks < 0:
        raise ValueError("remaining_blocks must be nonnegative")
    if estimated_task_bytes <= 0:
        raise ValueError("estimated_task_bytes must be positive")

    cpu_limit = max(1, dask.system.cpu_count() - 1)
    available = _available_bytes()
    budget = int(available * memory_fraction)
    memory_limit = max(1, budget // estimated_task_bytes)
    workers = min(cpu_limit, memory_limit, remaining_blocks)
    if requested != "auto":
        workers = min(workers, requested)
    return WorkerSelection(
        workers=workers,
        cpu_limit=cpu_limit,
        memory_limit=memory_limit,
        available_bytes=available,
        budget_bytes=budget,
        estimated_task_bytes=estimated_task_bytes,
        over_budget=estimated_task_bytes > budget,
    )


def estimate_task_bytes(
    ref_steps: int,
    hist_steps: int,
    sim_steps: int,
    cells: int,
    *,
    nquantiles: int = 50,
    groups: int = 12,
) -> int:
    """Estimate peak per-task RAM, not a guarantee or measured upper bound.

    Calibrated on CNRM-ESM2-1 + OBS.Vietnam (tmax, QDM, monthly groups,
    50 quantiles): peak RSS grew by ~1.56 MiB per cell, i.e. ~4x the float64
    reference/history/simulation inputs (copies, aligned/jittered series,
    corrected output and evaluation), on top of ~0.75 GiB for Python,
    xsdba and open files. Three grouped quantile arrays are added. Other
    variables, methods and allocators can exceed this heuristic.
    """
    if min(ref_steps, hist_steps, sim_steps, cells, nquantiles, groups) < 0:
        raise ValueError("task dimensions must be nonnegative")
    inputs = (ref_steps + hist_steps + sim_steps) * cells * 8
    quantiles = cells * nquantiles * groups * 8 * 3
    return int(inputs * _INPUT_FACTOR + quantiles + _WORKER_BASELINE)


_INPUT_FACTOR = 4
_WORKER_BASELINE = 768 * 1024**2


def _ignore_sigint() -> None:
    """Workers ignore Ctrl+C: the parent decides when to stop, at a batch boundary."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


class WorkerPool:
    """One process pool reused by every batch of a variable.

    dask's "processes" scheduler otherwise creates and shuts down a pool on
    each compute() call, so every batch pays process start-up and the
    import of xsdba again (~2.5 s per worker on Linux, more on Windows).
    """

    def __init__(self) -> None:
        self._pool: ProcessPoolExecutor | None = None
        self.size = 0

    def get(self, workers: int) -> ProcessPoolExecutor:
        """A pool with at least `workers` processes (recreated only to grow)."""
        if self._pool is None or workers > self.size:
            self.close()
            for key in _SINGLE_THREAD_ENV:  # inherited by the spawned workers
                os.environ.setdefault(key, "1")
            # Same fixed hash seed dask sets for its own pools.
            os.environ.setdefault("PYTHONHASHSEED", "6640")
            self._pool = ProcessPoolExecutor(
                workers, mp_context=mp.get_context("spawn"), initializer=_ignore_sigint
            )
            self.size = workers
        return self._pool

    def close(self, *, force: bool = False) -> None:
        """Shut the pool down; force=True also stops tasks still running."""
        if self._pool is None:
            return
        if force:
            # Workers ignore SIGINT, so stop them explicitly on a forced exit.
            for process in list(getattr(self._pool, "_processes", {}).values()):
                process.terminate()
        self._pool.shutdown(wait=not force, cancel_futures=True)
        self._pool, self.size = None, 0

    def __enter__(self) -> WorkerPool:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Any exception other than a normal error (e.g. a forced Ctrl+C)
        # must not wait for workers that ignore Ctrl+C.
        self.close(force=exc_type is not None and not issubclass(exc_type, Exception))
