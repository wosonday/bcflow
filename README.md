# bcflow

Pipeline hiệu chỉnh sai số khí hậu/thủy văn (bias correction), xây trên
[xsdba](https://github.com/Ouranosinc/xsdba), điều khiển bằng một file cấu hình
YAML hoặc JSON thay vì sửa code Python trực tiếp.

## Ý tưởng thiết kế

- **Tách cấu hình khỏi thực thi**: ai đó chọn thuật toán, tham số qua
  `config.yaml` (sau này: qua frontend); code Python cố định chỉ đọc và
  thực thi config đó.
- **Registry tường minh** (`methods.py`): chuỗi trong config ánh xạ tới
  class `xsdba` thật qua một dict cố định — không dùng `eval()`/`getattr`
  với input tuỳ ý.
- **QACheck xuyên suốt pipeline** (`qa.py`): mọi cảnh báo/lỗi là dữ liệu có
  cấu trúc đi theo kết quả, không phải `print`/`warnings.warn()` biến mất
  vào log. Đây là điều frontend sẽ hiển thị thành bảng pass/warn/fail.
- **Validate sớm** (`config_schema.py`, dùng pydantic): lỗi cấu hình hiện
  ra ngay khi nạp `config.yaml`, trước khi mở bất kỳ file dữ liệu nào.

## Cài đặt

```bash
uv sync
```

## Chạy

```bash
uv run python scripts/make_toy_data.py           # sinh dữ liệu mẫu vào data/toy/
uv run bcflow validate configs/example_toy.yaml  # kiểm tra config, không chạy
uv run bcflow run configs/example_toy.yaml       # tham chiếu là trạm
uv run bcflow run configs/example_toy.json       # cùng config, dạng JSON
uv run bcflow run configs/example_grid.yaml      # tham chiếu là lưới quan trắc
uv run bcflow inspect data/toy/model_hist_pr.nc  # xem biến, đơn vị, khoảng thời gian của file
uv run bcflow methods                            # các phương pháp hiệu chỉnh có sẵn
```

### Live progress and cancellation

`bcflow run CONFIG` reports progress to stderr while it runs. In an interactive
terminal, a `tqdm` bar shows completed/total cells, percentage, elapsed time, speed,
and estimated remaining time, with the current block and processing stage beside
it. Reference scanning uses a separate time-step bar. Redirected/non-interactive
output keeps plain progress lines, and `--json` never creates a progress bar.

Only completed blocks advance the cell bar; stage changes update its label.
The percentage measures completed cells, not total runtime; scanning, combining,
and writing are separate stages. The remaining-time estimate is approximate,
especially when block sizes or processing times vary. Use `grid.block_size` to
control the number of cells processed per block.

- Press **Ctrl+C once** to request cancellation. The current library operation
  may need to finish before the next cancellation checkpoint.
- Fully completed variables are saved. The interrupted variable's blocks are
  discarded, and remaining variables are skipped. This is not block checkpointing
  or resume support.
- Saving completed results is allowed to finish even after cancellation.
  Press **Ctrl+C again** to force exit; output files may then be incomplete.
- A safely cancelled run exits with **130**. Its manifest has `status: cancelled`
  and distinguishes `ok`, `failed`, `cancelled`, and `skipped` variables.
- A GUI (or any parent process) can cancel with `--cancel-file PATH`: the run
  stops at the next checkpoint once `PATH` exists, exactly as after one Ctrl+C.
  Use this from the desktop app — on Windows a parent process cannot reliably
  send Ctrl+C to a child.
- With `--json`, progress remains JSON lines on stdout; terminal messages go to
  stderr. The final `run_finished` event includes the cancellation status and
  the path to saved results. A forced exit reports `forced: true`, without
  claiming that outputs were safely saved.

### CPU/RAM-aware grid workers

Gridded runs default to automatic Dask process workers. Add these optional fields
inside your existing `grid:` section:

```yaml
workers: auto # or a positive integer cap; 1 keeps sequential stage progress
memory_fraction: 0.6 # fraction of currently available RAM used for the batch estimate
```

Concurrency is the minimum of usable CPU count minus **1** (at least one), the
RAM-based limit, the remaining block count, and any explicit worker cap. The
CPU count respects affinity/quota limits; RAM detection also checks conventional
Linux cgroup limits where accessible. Available RAM is checked before each batch.
Worker-selection events report the chosen count, limits, estimated task size,
and budget in both CLI and JSON output.

The memory estimate is about 4× the float64 inputs of a block plus ~0.75 GiB per
worker, calibrated on CNRM-ESM2-1 + OBS.Vietnam (tmax: 287 cells peaked at
1.19 GiB, 680 cells at 1.79 GiB). It is a heuristic, **not an out-of-memory guarantee**.
If one block exceeds the budget or available RAM is unknown, the run warns and
falls back to one worker; reduce `grid.block_size` if necessary. Completed block
results still accumulate in the main process.

Loading/regridding stays in the parent; workers train, adjust, and evaluate spatial
blocks while keeping their time records together. One process pool is reused by
every batch of a variable (workers start and import xsdba once), each worker uses
one BLAS/OpenMP thread, and workers ignore Ctrl+C so the parent decides when to
stop. The model hist is cut to the reference's years before regridding (e.g.
1980–2014 of a 1850–2014 CMIP run): ~27% faster with identical results. Parallel results are collected
in block order. Progress and cooperative cancellation occur at batch boundaries,
not inside worker training. The first Ctrl+C may therefore wait for the active
batch, and forced cancellation can also be delayed by process-pool cleanup.
Use `workers: 1` for the previous sequential behavior. This setting currently
applies only to gridded references, not station runs. Benchmark on a small region:
more workers can be slower when disk access or process-transfer overhead dominates.

```bash
uv run python scripts/bench_workers.py configs/cnrm_obs_vietnam.yaml --variable tmax
```

prints the time with `workers: 1` and `workers: auto` (after a warm-up run that
fills the file cache), the workers chosen per batch, and whether both results are
identical.

Library callers can pass `RunControl` from `bcflow.progress` to
`run_config_file()` or `run_pipeline()`, and call `request_cancel()` from a
progress callback or another thread. Signal handlers are installed only by the CLI.

Config mẫu trong `configs/`:

| File                         | Tham chiếu             | Dữ liệu                                      |
| ---------------------------- | ---------------------- | -------------------------------------------- |
| `example_toy.yaml` / `.json` | trạm (bố cục swatprep) | toy, sinh bằng `scripts/make_toy_data.py`    |
| `example_grid.yaml`          | lưới quan trắc 0,5°    | toy, sinh bằng `scripts/make_toy_data.py`    |
| `cnrm_obs_vietnam.yaml`      | lưới OBS.Vietnam 0,1°  | thật: `data/OBS.Vietnam.1980-2014.nc` + CNRM |

## Dữ liệu đầu vào

Dữ liệu trạm theo đúng bố cục của
[swat-preprocessing](https://github.com/wosonday/swat-preprocessing), nên một
thư mục dùng chung được cho cả hai công cụ:

```
weather_stations.csv     id,name,lat,lon,elev
weather/ST001.csv        date,precip,tmax,tmin[,wind,solar,humidity]
```

Đơn vị dữ liệu trạm theo quy ước swatprep: precip mm/ngày, tmax/tmin °C,
wind m/s, solar MJ/m²/ngày, humidity phân số 0–1. Dữ liệu mô hình (NetCDF/Zarr)
được đổi về các đơn vị này theo thuộc tính `units` của file.

```yaml
stations:
    metadata: data/weather_stations.csv
    data_dir: data/weather
    file_pattern: "{id}.csv" # hoặc "{name}.csv"
variables: [precip, tmax, tmin] # thêm wind, solar, humidity nếu có
model:
    hist: data/GCMs/CNRM-ESM2-1_historical_r1i1p1f2_{var}_gr.nc # {var} -> tên trong var_map
    sim: data/GCMs/CNRM-ESM2-1_ssp245_r1i1p1f2_{var}_gr.nc # không có {var}: một file chứa mọi biến
    var_map: { precip: pr, tmax: tasmax, tmin: tasmin }
    lat_name: auto
    lon_name: auto
    time_name: auto
    search_radius: 1 # số ô lưới tìm quanh khi ô gần nhất không có dữ liệu
periods:
    calibration: ["1980", "2004"] # nên >= 30 năm (chuẩn khí hậu WMO)
    validation: ["2005", "2014"]
    min_coverage: 0.8 # tỷ lệ ngày có số liệu tối thiểu của mỗi trạm trong thời kỳ hiệu chuẩn
correction: # tùy chọn: ghi đè mặc định của từng biến (variables.py)
    tmax: { group: dayofyear, window: 31 } # dayofyear | month | none
```

Mặc định mọi biến dùng Quantile Delta Mapping, nhóm theo tháng; precip thêm
`adapt_freq` với ngưỡng ngày mưa 1 mm/ngày.

Trạm thiếu số liệu hiệu chuẩn được cảnh báo sớm (`coverage.py`): `bcflow validate`
chỉ đọc dữ liệu trạm (vài giây, không mở file mô hình) và liệt kê các trạm có tỷ lệ
ngày hợp lệ dưới `min_coverage`, cùng cảnh báo khi thời kỳ hiệu chuẩn ngắn hơn 30 năm.
Các trạm này vẫn được hiệu chỉnh; trạm không có ngày nào hợp lệ sẽ cho chuỗi rỗng (NaN).

### Tham chiếu là lưới quan trắc (thay cho `stations:`)

```yaml
grid:
    path: data/OBS.Vietnam.1980-2014.nc
    var_map: { precip: pr, tmax: tasmax, tmin: tasmin }
    units: { precip: "mm d-1", tmax: degC, tmin: degC } # khi file ghi "var_unit" (CDO)
    region: { lat: [20.0, 21.5], lon: [105.0, 107.0] } # tùy chọn: chỉ một vùng/lưu vực
    block_size: 500 # số ô hiệu chỉnh mỗi lượt (giảm nếu thiếu RAM)
correction:
    tmax: { regrid: bicubic } # nearest | idw | bilinear | bicubic | conservative | patch
    precip: { regrid: idw, regrid_params: { neighbours: 4, power: 2 } }
```

GCM được regrid về lưới tham chiếu (`regrid.py`), rồi mỗi ô được hiệu chỉnh như một
trạm. Mặc định: precip `conservative` (giữ tổng lượng mưa theo diện tích), các biến
khác `bilinear`. Kết quả `scen.nc`, `hist_eval.nc` có dạng `(time, lat, lon)` trên lưới
tham chiếu; ô không có số liệu (biển) để trống. CSV (nếu chọn) ở dạng dài
`time, lat, lon, giá trị`, chỉ gồm các ô được hiệu chỉnh — file có thể rất lớn.

## Cấu trúc

```
src/bcflow/
    qa.py             QACheck — kết quả kiểm tra có cấu trúc
    variables.py      Mặc định hiệu chỉnh cho từng biến swatprep (precip, tmax, ...)
    config_schema.py  Validate config bằng pydantic (stations | grid, model, periods, correction, output)
    config_loader.py  Đọc config YAML/JSON, chuyển đường dẫn tương đối theo vị trí file
    grouping.py       Grouper của xsdba + tự nhận diện lat/lon
    methods.py        Ánh xạ tên phương pháp -> class xsdba
    extract.py        Ô lưới gần trạm nhất (lưới đều hoặc cong), tìm ô lân cận khi ô trống
    loaders.py        Đọc dữ liệu trạm (bố cục swatprep) + file mô hình NetCDF/Zarr
    timeaxis.py       Đưa trạm và mô hình về một trục thời gian ngày chung
    coverage.py       Cảnh báo sớm trạm/ô thiếu số liệu hiệu chuẩn, thời kỳ < 30 năm
    regrid.py         Regrid GCM về lưới quan trắc: nearest, idw, bilinear, bicubic,
                      conservative, patch
    units.py          Kiểm tra và đổi đơn vị (K -> degC, kg m-2 s-1 -> mm/d)
    evaluate.py       Đánh giá trước/sau hiệu chỉnh, so sánh theo phân phối
    adjust.py         run_variable — mọi bước cho một biến
    pipeline.py       run_pipeline — chạy mọi biến, cô lập lỗi, phát sự kiện tiến độ
    writers.py        Ghi kết quả, qa.json, config.json, manifest.json
    service.py        Các thao tác cho giao diện, trả dữ liệu sẵn sàng thành JSON
    cli.py            Lệnh `bcflow run | validate | inspect | methods | schema`
scripts/
    make_toy_data.py  Sinh dữ liệu giả để chạy thử pipeline
tests/
    test_*.py         pytest
configs/
    example_toy.yaml       Config mẫu, tham chiếu là trạm (dữ liệu toy)
    example_toy.json       Cùng config ở dạng JSON
    example_grid.yaml      Config mẫu, tham chiếu là lưới quan trắc (dữ liệu toy)
    cnrm_obs_vietnam.yaml  Config cho dữ liệu thật: CNRM-ESM2-1 + OBS.Vietnam
```

## Giao tiếp với frontend (JSON)

Ứng dụng desktop (GPUI/Rust) gọi `bcflow` như một tiến trình con và đọc stdout.
Với `--json`, stdout chỉ chứa JSON (thuần ASCII, không phụ thuộc bảng mã của
Windows); cảnh báo của thư viện và traceback đi ra stderr.

| Lệnh                            | stdout khi có `--json`                                       | Mã thoát                                                      |
| ------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------- |
| `bcflow run CONFIG --json`      | mỗi dòng một sự kiện JSON; dòng cuối là `run_finished`       | 0 thành công, 1 có biến lỗi, 2 không chạy được, 130 cancelled |
| `bcflow validate CONFIG --json` | `{"valid", "variables", "errors": [...], "warnings": [...]}` | 0 hợp lệ (có thể kèm cảnh báo), 1 không hợp lệ                |
| `bcflow inspect PATH --json`    | biến, chiều, đơn vị, khoảng thời gian của file/thư mục       | 0, hoặc 2 kèm `{"error"}`                                     |
| `bcflow methods --json`         | `[{"name", "class", "summary"}]`                             | 0                                                             |
| `bcflow schema`                 | JSON Schema của file config                                  | 0                                                             |

Các sự kiện của `run`, theo thứ tự:

```json
{"event": "run_started", "variables": ["precip", "tmax"]}
{"event": "variable_started", "variable": "precip", "index": 1, "total": 2}
{"event": "progress", "variable": "precip", "stage": "block_finished", "block_index": 1, "total_blocks": 20, "completed_cells": 500, "total_cells": 10000, "block_cells": 500, "elapsed_seconds": 42.5}
{"event": "qa", "variable": "precip", "check": {"name": "...", "status": "pass", "message": "...", "context": {}}}
{"event": "variable_finished", "variable": "precip", "status": "ok"}
{"event": "run_finished", "status": "ok", "exit_code": 0, "error": null, "run_dir": "...", "manifest": "...",
 "variables": {"precip": "ok", "tmax": "ok"}, "counts": {"pass": 13, "warn": 0, "fail": 0}, "qa": ["..."]}
```

Mỗi lần chạy tạo một thư mục `outputs/run_YYYYmmdd_HHMMSS/` gồm `manifest.json`
(đường dẫn tương đối), `qa.json`, `config.json` và với mỗi biến: `scen.nc`
(+ `scen.csv`), `adj.nc`, `evaluation.csv`, `hist_eval.nc` (+ `hist_eval.csv`). Cấu trúc `manifest.json` được mô tả
trong docstring của `writers.py` (`schema_version: 1`). Với tham chiếu là lưới,
`scen.nc` và `hist_eval.nc` có dạng `(time, lat, lon)`.
