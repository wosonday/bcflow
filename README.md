# bcflow

Pipeline hiệu chỉnh sai số khí hậu/thủy văn (bias correction), xây trên
[xsdba](https://github.com/Ouranosinc/xsdba), điều khiển bằng `config.yaml`
thay vì sửa code Python trực tiếp.

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
uv run python scripts/make_toy_data.py          # sinh dữ liệu mẫu vào data/toy/
uv run bcflow validate configs/example_toy.yaml # kiểm tra config, không chạy
uv run bcflow run configs/example_toy.yaml      # chạy và ghi kết quả vào outputs/
uv run bcflow inspect data/toy/model_hist_pr.nc # xem biến, đơn vị, khoảng thời gian của file
uv run bcflow methods                           # các phương pháp hiệu chỉnh có sẵn
```

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
  file_pattern: "{id}.csv"          # hoặc "{name}.csv"
variables: [precip, tmax, tmin]     # thêm wind, solar, humidity nếu có
model:
  hist: data/GCMs/CNRM-ESM2-1_historical_r1i1p1f2_{var}_gr.nc   # {var} -> tên trong var_map
  sim: data/GCMs/CNRM-ESM2-1_ssp245_r1i1p1f2_{var}_gr.nc        # không có {var}: một file chứa mọi biến
  var_map: { precip: pr, tmax: tasmax, tmin: tasmin }
  lat_name: auto
  lon_name: auto
  time_name: auto
  search_radius: 1                  # số ô lưới tìm quanh khi ô gần nhất không có dữ liệu
periods:
  calibration: ["1980", "2004"]
  validation: ["2005", "2014"]
correction:                         # tùy chọn: ghi đè mặc định của từng biến (variables.py)
  tmax: { group: dayofyear, window: 31 }   # dayofyear | month | none
```

Mặc định mọi biến dùng Quantile Delta Mapping, nhóm theo tháng; precip thêm
`adapt_freq` với ngưỡng ngày mưa 1 mm/ngày.

## Cấu trúc

```
src/bcflow/
    qa.py             QACheck — kết quả kiểm tra có cấu trúc
    variables.py      Mặc định hiệu chỉnh cho từng biến swatprep (precip, tmax, ...)
    config_schema.py  Validate config.yaml bằng pydantic (stations, model, periods, correction, output)
    config_loader.py  Đọc config.yaml, chuyển đường dẫn tương đối theo vị trí file
    grouping.py       Grouper của xsdba + tự nhận diện lat/lon
    methods.py        Ánh xạ tên phương pháp -> class xsdba
    extract.py        Ô lưới gần trạm nhất (lưới đều hoặc cong), tìm ô lân cận khi ô trống
    loaders.py        Đọc dữ liệu trạm (bố cục swatprep) + file mô hình NetCDF/Zarr
    timeaxis.py       Đưa trạm và mô hình về một trục thời gian ngày chung
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
    example_toy.yaml  Config mẫu khớp với dữ liệu toy
```

## Giao tiếp với frontend (JSON)

Ứng dụng desktop (GPUI/Rust) gọi `bcflow` như một tiến trình con và đọc stdout.
Với `--json`, stdout chỉ chứa JSON (thuần ASCII, không phụ thuộc bảng mã của
Windows); cảnh báo của thư viện và traceback đi ra stderr.

| Lệnh | stdout khi có `--json` | Mã thoát |
|---|---|---|
| `bcflow run CONFIG --json` | mỗi dòng một sự kiện JSON; dòng cuối là `run_finished` | 0 thành công, 1 có biến lỗi, 2 không chạy được |
| `bcflow validate CONFIG --json` | `{"valid", "variables", "errors": [{"variable", "loc", "message"}]}` | 0 hợp lệ, 1 không hợp lệ |
| `bcflow inspect PATH --json` | biến, chiều, đơn vị, khoảng thời gian của file/thư mục | 0, hoặc 2 kèm `{"error"}` |
| `bcflow methods --json` | `[{"name", "class", "summary"}]` | 0 |
| `bcflow schema` | JSON Schema của `config.yaml` | 0 |

Các sự kiện của `run`, theo thứ tự:

```json
{"event": "run_started", "variables": ["precip", "tmax"]}
{"event": "variable_started", "variable": "precip", "index": 1, "total": 2}
{"event": "qa", "variable": "precip", "check": {"name": "...", "status": "pass", "message": "...", "context": {}}}
{"event": "variable_finished", "variable": "precip", "status": "ok"}
{"event": "run_finished", "status": "ok", "exit_code": 0, "error": null, "run_dir": "...", "manifest": "...",
 "variables": {"precip": "ok", "tmax": "ok"}, "counts": {"pass": 13, "warn": 0, "fail": 0}, "qa": ["..."]}
```

Mỗi lần chạy tạo một thư mục `outputs/run_YYYYmmdd_HHMMSS/` gồm `manifest.json`
(đường dẫn tương đối), `qa.json`, `config.json` và với mỗi biến: `scen.nc`
(+ `scen.csv`), `adj.nc`, `evaluation.csv`, `hist_eval.nc` (+ `hist_eval.csv`). Cấu trúc `manifest.json` được mô tả
trong docstring của `writers.py` (`schema_version: 1`).
