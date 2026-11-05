# bcflow

Pipeline hiệu chỉnh sai số khí hậu/thủy văn (bias correction), xây trên
[xsdba](https://github.com/Ouranosinc/xsdba), điều khiển bằng `config.yaml`
thay vì sửa code Python trực tiếp.

## Ý tưởng thiết kế

- **Tách cấu hình khỏi thực thi**: ai đó chọn thuật toán, tham số qua
  `config.yaml` (sau này: qua frontend); code Python cố định chỉ đọc và
  thực thi config đó.
- **Registry tường minh** (`methods.py`, `extract.py`): chuỗi trong config ánh xạ tới
  class `xsdba` thật qua một dict cố định — không dùng `eval()`/`getattr`
  với input tuỳ ý.
- **QACheck xuyên suốt pipeline** (`qa.py`): mọi cảnh báo/lỗi là dữ liệu có
  cấu trúc đi theo kết quả, không phải `print`/`warnings.warn()` biến mất
  vào log. Đây là điều frontend sẽ hiển thị thành bảng pass/warn/fail.
- **Validate sớm** (`config_schema.py`, dùng pydantic): lỗi cấu hình hiện
  ra ngay khi nạp `config.yaml`, trước khi mở bất kỳ file dữ liệu nào.

## Cài đặt

```bash
uv sync --extra dev
```

## Chạy

```bash
uv run python scripts/make_toy_data.py          # sinh dữ liệu mẫu vào data/
uv run bcflow validate configs/example_toy.yaml # kiểm tra config, không chạy
uv run bcflow run configs/example_toy.yaml      # chạy và ghi kết quả vào outputs/
uv run bcflow inspect data/pr_hist.nc           # xem biến, đơn vị, khoảng thời gian của file
uv run bcflow methods                           # các phương pháp hiệu chỉnh có sẵn
```

## Cấu trúc

```
src/bcflow/
    qa.py             QACheck — kết quả kiểm tra có cấu trúc
    config_schema.py  Validate config.yaml bằng pydantic (gồm periods, output)
    config_loader.py  Đọc config.yaml, chuyển đường dẫn tương đối theo vị trí file
    grouping.py       Grouper của xsdba + tự nhận diện lat/lon
    methods.py        Ánh xạ tên phương pháp -> class xsdba
    extract.py        Trích ô lưới gần nhất (regular_latlon, rotated_pole)
    loaders.py        Đọc CSV điểm trạm (long/wide) + raster NetCDF/Zarr
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
{"event": "run_started", "variables": ["tasmax", "pr"]}
{"event": "variable_started", "variable": "tasmax", "index": 1, "total": 2}
{"event": "qa", "variable": "tasmax", "check": {"name": "...", "status": "pass", "message": "...", "context": {}}}
{"event": "variable_finished", "variable": "tasmax", "status": "ok"}
{"event": "run_finished", "status": "ok", "exit_code": 0, "error": null, "run_dir": "...", "manifest": "...",
 "variables": {"tasmax": "ok", "pr": "ok"}, "counts": {"pass": 13, "warn": 0, "fail": 0}, "qa": ["..."]}
```

Mỗi lần chạy tạo một thư mục `outputs/run_YYYYmmdd_HHMMSS/` gồm `manifest.json`
(đường dẫn tương đối), `qa.json`, `config.json` và với mỗi biến: `scen.nc`
(+ `scen.csv`), `adj.nc`, `evaluation.csv`. Cấu trúc `manifest.json` được mô tả
trong docstring của `writers.py` (`schema_version: 1`).
