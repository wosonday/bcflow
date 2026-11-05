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
uv run bcflow configs/example_toy.yaml
```

## Cấu trúc

```
src/bcflow/
    qa.py             QACheck — kết quả kiểm tra có cấu trúc
    config_schema.py  Validate config.yaml bằng pydantic
    config_loader.py  Đọc config.yaml, chuyển đường dẫn tương đối theo vị trí file
    grouping.py       Grouper của xsdba + tự nhận diện lat/lon
    methods.py        Ánh xạ tên phương pháp -> class xsdba
    extract.py        Trích ô lưới gần nhất (regular_latlon, rotated_pole)
    loaders.py        Đọc CSV điểm trạm (long/wide) + raster NetCDF/Zarr
    adjust.py         run_variable — train/adjust cho một biến
    pipeline.py       run_pipeline — chạy mọi biến, cô lập lỗi theo biến
    writers.py        Ghi kết quả (NetCDF/CSV) + manifest
    cli.py            `bcflow <config.yaml>`
scripts/
    make_toy_data.py  Sinh dữ liệu giả để chạy thử pipeline
tests/
    test_*.py         pytest
configs/
    example_toy.yaml  Config mẫu khớp với dữ liệu toy
```
