"""Compare one worker with automatic workers on a gridded config.

Runs the first variable of the config on a small region, without writing
outputs: once with grid.workers = 1 to warm the file cache (not counted),
then "auto", then 1 again, and prints both timings, the workers chosen and
whether the results are identical.

    uv run python scripts/bench_workers.py configs/cnrm_obs_vietnam.yaml
    uv run python scripts/bench_workers.py configs/cnrm_obs_vietnam.yaml --variable tmax --block-size 250

Use the numbers to choose grid.workers / grid.block_size for full runs.
"""

from __future__ import annotations

import argparse
import copy
import time

import numpy as np
import psutil

from bcflow.config_loader import load_config_file
from bcflow.config_schema import load_config
from bcflow.pipeline import run_pipeline
from bcflow.progress import RunControl


def _run(raw: dict, variable: str, workers: int | str) -> dict:
    cfg = copy.deepcopy(raw)
    cfg["grid"]["workers"] = workers
    chosen: list[int] = []

    def on_event(event: dict) -> None:
        if event.get("stage") == "worker_selection":
            chosen.append(event["workers"])

    started = time.perf_counter()
    out = run_pipeline(load_config(cfg), control=RunControl(on_event))
    elapsed = time.perf_counter() - started
    result = out["results"][variable]
    if result is None:
        failures = [c.message for c in out["qa"] if c.status == "fail"]
        raise SystemExit(f"{variable} failed with workers={workers}: {failures}")
    return {"seconds": elapsed, "workers": chosen, "scen": result["scen"]}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare one worker with automatic workers on a gridded config."
    )
    parser.add_argument("config", help="a config with a 'grid:' reference")
    parser.add_argument("--variable", help="default: the config's first variable")
    parser.add_argument(
        "--lat", nargs=2, type=float, default=[20.0, 21.5], metavar=("MIN", "MAX")
    )
    parser.add_argument(
        "--lon", nargs=2, type=float, default=[105.0, 107.0], metavar=("MIN", "MAX")
    )
    parser.add_argument("--block-size", type=int, default=100)
    args = parser.parse_args()

    raw = load_config_file(args.config)
    if "grid" not in raw:
        raise SystemExit(
            "This benchmark needs a gridded reference ('grid:' in the config)."
        )
    variable = args.variable or raw["variables"][0]
    raw["variables"] = [variable]
    raw["correction"] = {
        k: v for k, v in raw.get("correction", {}).items() if k == variable
    }
    raw["grid"]["region"] = {"lat": args.lat, "lon": args.lon}
    raw["grid"]["block_size"] = args.block_size

    print(
        f"CPU: {psutil.cpu_count()} logical | RAM available: "
        f"{psutil.virtual_memory().available / 1024**3:.1f} GiB | variable: {variable} | "
        f"region lat {args.lat} lon {args.lon} | block_size {args.block_size}"
    )
    warm = _run(raw, variable, 1)
    print(f"warm-up      : {warm['seconds']:7.1f} s  (reads the files into the cache)")
    auto = _run(raw, variable, "auto")
    print(
        f"workers=auto : {auto['seconds']:7.1f} s  (chosen per batch: {auto['workers']})"
    )
    one = _run(raw, variable, 1)
    print(f"workers=1    : {one['seconds']:7.1f} s")
    same = np.array_equal(one["scen"].values, auto["scen"].values, equal_nan=True)
    print(
        f"speed-up x{one['seconds'] / auto['seconds']:.2f} | cells {one['scen'].sizes['station']} | "
        f"identical results: {same}"
    )


if __name__ == "__main__":
    main()
