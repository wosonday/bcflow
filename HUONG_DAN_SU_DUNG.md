# Hướng dẫn sử dụng dự án bias-correction / bcflow

Tài liệu tổng hợp từ mã nguồn, cấu hình và các README trong thư mục dự án, ngày 25/11/2026. Các lệnh bên dưới chạy tại thư mục gốc `D:\Project\bias-correction` bằng PowerShell, trừ khi có chỉ dẫn khác.

## 1. Dự án gồm những gì?

`bcflow` là pipeline Python hiệu chỉnh sai số dữ liệu khí hậu/thủy văn dựa trên `xsdba`. Người dùng chọn dữ liệu, biến và phương pháp bằng YAML hoặc JSON.

| Thành phần | Vị trí | Cách sử dụng |
|---|---|---|
| Bộ xử lý khoa học và CLI | `src/bcflow/` | Lệnh `bcflow` để kiểm tra cấu hình, hiệu chỉnh và xuất kết quả |
| Giao diện web Streamlit | `app/` | Mở cấu hình, sửa bản nháp, kiểm tra, chạy và xem kết quả trên trình duyệt |
| Cấu hình mẫu | `configs/` | Chạy thử dữ liệu trạm, dữ liệu lưới hoặc CNRM + OBS.Vietnam |
| Công cụ hỗ trợ | `scripts/` | Sinh dữ liệu thử, so sánh workers và kiểm tra tài liệu harness |
| Kiểm thử | `tests/` | Kiểm tra backend, cấu hình, xuất dữ liệu và giao diện |
| Tài liệu phối hợp AI | `AGENTS-TUTORIAL.md`, `BOT-CEO.md`, `HERDR-CORE-BOT.md`, `docs/` | Hướng dẫn công cụ và quy trình hỗ trợ phát triển; không bắt buộc để chạy hiệu chỉnh |

README còn nhắc `learning/rust-desktop/`, nhưng thư mục này không có trong bản làm việc được khảo sát. Hướng dẫn này chỉ cung cấp lệnh cho backend và web hiện có; không coi Rust desktop là ứng dụng đã sẵn sàng sử dụng.

## 2. Cài đặt

Cần Python **3.14 trở lên** và công cụ **uv** có sẵn trong PATH. Phiên bản yêu cầu được khai báo trong [pyproject.toml](pyproject.toml).

### Windows PowerShell

```powershell
Set-Location D:\Project\bias-correction

# Dùng môi trường Windows riêng, đặc biệt nếu .venv được tạo từ WSL.
$env:UV_PROJECT_ENVIRONMENT = '.venv-windows'

# Chỉ dùng dòng lệnh:
uv sync

# Hoặc cài cả giao diện web:
uv sync --extra web
```

Biến `UV_PROJECT_ENVIRONMENT` ở trên chỉ có hiệu lực trong phiên PowerShell hiện tại. Đặt lại biến khi mở terminal mới nếu muốn tiếp tục dùng `.venv-windows`. Không dùng chung một virtualenv giữa Windows và WSL.

Kiểm tra CLI sau khi cài:

```powershell
uv run bcflow --help
uv run bcflow --version
uv run bcflow methods
```

## 3. Chạy thử nhanh bằng dữ liệu mẫu

### Hiệu chỉnh theo trạm

```powershell
# Sinh dữ liệu mẫu vào data/toy/.
uv run python scripts/make_toy_data.py

# Kiểm tra cấu hình trước khi chạy.
uv run bcflow validate configs/example_toy.yaml

# Chạy hiệu chỉnh precip và tmax.
uv run bcflow run configs/example_toy.yaml
```

Cấu hình này dùng giai đoạn hiệu chuẩn 2000–2012 và kiểm định 2013–2020. Cảnh báo thời kỳ hiệu chuẩn dưới 30 năm là dự kiến đối với ví dụ ngắn này. Kết quả được ghi vào một thư mục mới dưới `outputs/`.

Ví dụ JSON tương đương:

```powershell
uv run bcflow validate configs/example_toy.json
uv run bcflow run configs/example_toy.json
```

### Hiệu chỉnh theo lưới quan trắc

Sau khi sinh dữ liệu mẫu ở bước trên:

```powershell
uv run bcflow validate configs/example_grid.yaml
uv run bcflow run configs/example_grid.yaml
```

Pipeline đưa lưới mô hình về lưới quan trắc, rồi hiệu chỉnh từng ô có dữ liệu. Ví dụ lưới mặc định chỉ xuất NetCDF để hạn chế dung lượng.

## 4. Sử dụng giao diện web

```powershell
uv sync --extra web
uv run --extra web python -m app.launch
```

Tự mở trình duyệt tại **http://localhost:8501**. Launcher không tự mở trình duyệt. Nếu cổng đã được dùng:

```powershell
uv run --extra web python -m app.launch --server.port=8502
```

Khi đó truy cập **http://localhost:8502**.

### Quy trình thao tác

1. Vào **Configure**, chọn cấu hình mẫu hoặc nhập đường dẫn YAML/JSON và bấm **Open config**.
2. Sửa bản nháp nếu cần. Thao tác chỉnh sửa không tự lưu đè cấu hình gốc.
3. Bấm **Validate config**, theo dõi lỗi và cảnh báo tại phần giám sát.
4. Sau khi kiểm tra thành công, trở lại Configure và bấm **Run correction**. Nút chạy chỉ được bật cho đúng bản nháp đã kiểm tra; sửa tiếp thì phải kiểm tra lại.
5. Vào **Run & monitor** để xem biến đang xử lý, tiến độ ô lưới, workers, QA và log.
6. Vào **Results**, chọn thư mục kết quả hoặc đường dẫn `manifest.json` để xem trạng thái, QA và bảng đánh giá.

**Save copy** lưu bản cấu hình riêng. **Download resolved JSON** tải cấu hình với đường dẫn đã phân giải. Các bản sao chuẩn hóa không giữ chú thích YAML.

### Lưu ý khi dùng web

- Đây là ứng dụng cá nhân chạy ở localhost; đường dẫn nhập vào là đường dẫn trên máy chạy server.
- Mỗi thời điểm chỉ có một tác vụ được chạy trên toàn bộ các tab/phiên của ứng dụng.
- Làm mới trang không khởi động lại tác vụ, nhưng có thể mất bản nháp trong phiên trình duyệt.
- Khởi động lại server không nối lại tác vụ cũ. Nên đợi tác vụ kết thúc trước khi tắt server.
- Muốn dừng tác vụ, dùng **Request cancellation** và chờ xác nhận. Dừng có thể phải đợi phép tính hoặc batch hiện tại hoàn tất.
- Snapshot và log tác vụ nằm dưới `outputs/.web-jobs/<job-id>/`.
- Results chỉ xem trước tối đa 50.000 dòng đánh giá; biểu đồ hiển thị tối đa 100 vị trí cho một thời kỳ và chỉ số được chọn. Xem file xuất để phân tích đầy đủ.

Chi tiết: [app/README.md](app/README.md).

## 5. Chuẩn bị dữ liệu của bạn

Mỗi cấu hình phải chọn **đúng một** nguồn tham chiếu: `stations` hoặc `grid`. Cả hai cùng sử dụng `model.hist` để học quan hệ sai số và `model.sim` làm chuỗi cần hiệu chỉnh.

### Dữ liệu trạm

```text
data/my_stations/
  weather_stations.csv
  weather/
    ST001.csv
    ST002.csv
```

Ví dụ metadata:

```csv
id,name,lat,lon,elev
ST001,Tram_1,21.03,105.85,10
ST002,Tram_2,20.85,106.68,5
```

Ví dụ `weather/ST001.csv`:

```csv
date,precip,tmax,tmin
2000-01-01,0.0,23.5,17.2
2000-01-02,4.5,22.8,16.9
```

Đây chỉ là ví dụ định dạng; cần chuỗi đủ dài và bao phủ thời kỳ đã cấu hình để hiệu chỉnh có ý nghĩa. Cột `elev` trong metadata là tùy chọn.

| Tên biến trong cấu hình | Ý nghĩa | Đơn vị dữ liệu trạm |
|---|---|---|
| `precip` | Lượng mưa | mm/ngày |
| `tmax` | Nhiệt độ lớn nhất | °C |
| `tmin` | Nhiệt độ nhỏ nhất | °C |
| `wind` | Tốc độ gió | m/s |
| `solar` | Bức xạ | MJ/m²/ngày |
| `humidity` | Độ ẩm tương đối | Phân số 0–1 |

### Dữ liệu lưới và mô hình

Dữ liệu lưới tham chiếu và mô hình sử dụng NetCDF hoặc Zarr. `var_map` nối tên biến của bcflow với tên biến trong file, ví dụ `precip: pr`, `tmax: tasmax`, `tmin: tasmin`.

Nếu đường dẫn mô hình chứa `{var}`, bcflow thay nó bằng tên trong `model.var_map`. Nếu không chứa `{var}`, đường dẫn trỏ tới một bộ dữ liệu chứa các biến cần dùng.

Kiểm tra file trước khi cấu hình:

```powershell
uv run bcflow inspect data/toy/model_hist_pr.nc
uv run bcflow inspect data/toy/obs_grid.nc
```

Đơn vị mô hình được đọc từ thuộc tính `units` và chuyển về đơn vị đích. Khi thuộc tính thiếu hoặc sai, khai báo đơn vị thực bằng `model.units` hoặc `grid.units`; không chỉ đổi nhãn để che lỗi dữ liệu.

## 6. Tạo và chỉnh cấu hình

Sao chép cấu hình phù hợp để bắt đầu, ví dụ:

```powershell
Copy-Item configs/example_toy.yaml configs/my_stations.yaml
```

### Ví dụ cấu hình trạm

Lưu nội dung sau vào một file trong `configs/`, rồi thay đường dẫn và thời kỳ theo dữ liệu thực:

```yaml
stations:
  metadata: ../data/my_stations/weather_stations.csv
  data_dir: ../data/my_stations/weather
  file_pattern: "{id}.csv"

variables: [precip, tmax, tmin]

model:
  hist: ../data/my_model/historical_{var}.nc
  sim: ../data/my_model/ssp245_{var}.nc
  var_map: {precip: pr, tmax: tasmax, tmin: tasmin}

periods:
  calibration: ["1980", "2004"]
  validation: ["2005", "2014"]
  min_coverage: 0.8

correction:
  tmax:
    method: quantile_delta_mapping
    group: month

output:
  path: ../outputs/my_stations
  formats: [netcdf, csv]
```

### Các trường cần hiểu

| Trường | Công dụng |
|---|---|
| `stations` / `grid` | Chọn dữ liệu tham chiếu trạm hoặc lưới |
| `variables` | Danh sách biến cần hiệu chỉnh |
| `model.hist` | Dữ liệu mô hình lịch sử để huấn luyện và đánh giá |
| `model.sim` | Dữ liệu mô hình cần hiệu chỉnh, ví dụ kịch bản tương lai |
| `model.var_map` | Ánh xạ tên biến; phải có đủ các biến đã chọn |
| `periods.calibration` | Giai đoạn huấn luyện với quan trắc và mô hình lịch sử |
| `periods.validation` | Giai đoạn đánh giá độc lập, không được chồng với hiệu chuẩn |
| `periods.min_coverage` | Ngưỡng cảnh báo tỷ lệ ngày quan trắc hợp lệ; mặc định 0,8 |
| `correction.<biến>` | Ghi đè phương pháp, nhóm thời gian, tham số và nội suy lưới |
| `output.path` | Thư mục gốc chứa các lần chạy |
| `output.formats` | `netcdf`, `csv`, hoặc cả hai |

**Đường dẫn tương đối được khai báo trong cấu hình được tính từ thư mục chứa file cấu hình.** Ví dụ `../data/` trong `configs/my_stations.yaml` trỏ đến `data/` ở gốc dự án. Nếu bỏ hẳn `output.path`, giá trị mặc định `outputs` được dùng theo thư mục làm việc hiện tại; nên khai báo rõ để tránh nhầm.

Không dùng cùng các năm cho hiệu chuẩn và kiểm định. Nếu có `validation` thì phải có `calibration`. Nếu bỏ `validation`, đánh giá là trên tập huấn luyện, không phải kiểm định độc lập. Thời kỳ hiệu chuẩn dưới 30 năm sẽ có cảnh báo.

### Phương pháp và nhóm thời gian

Mặc định dùng `quantile_delta_mapping`, nhóm theo tháng. Mưa có thêm điều chỉnh tần suất ngày mưa với ngưỡng `1 mm d-1`. Xem danh sách phương pháp thực tế bằng:

```powershell
uv run bcflow methods
uv run bcflow schema
```

`group` hỗ trợ `month`, `dayofyear`, `season`, `none`. Ví dụ gộp dữ liệu quanh từng ngày trong năm:

```yaml
correction:
  tmax:
    group: dayofyear
    window: 31
```

Đây là đoạn để ghép vào cấu hình đầy đủ. Không dùng `window > 1` với `group: none`. Các tham số nâng cao gồm `train_params`, `adjust_params`, `pool_dims`, chuỗi hậu xử lý `post` và `custom_variables`; đối chiếu [schema hiện hành](src/bcflow/config_schema.py) trước khi sử dụng.

## 7. Chạy dữ liệu thật CNRM + OBS.Vietnam

Cấu hình sẵn có: [configs/cnrm_obs_vietnam.yaml](configs/cnrm_obs_vietnam.yaml).

```text
data/
  OBS.Vietnam.1980-2014.nc
  GCMs/
    CNRM-ESM2-1_historical_r1i1p1f2_pr_gr.nc
    CNRM-ESM2-1_historical_r1i1p1f2_tasmax_gr.nc
    CNRM-ESM2-1_historical_r1i1p1f2_tasmin_gr.nc
    CNRM-ESM2-1_ssp245_r1i1p1f2_pr_gr.nc
    CNRM-ESM2-1_ssp245_r1i1p1f2_tasmax_gr.nc
    CNRM-ESM2-1_ssp245_r1i1p1f2_tasmin_gr.nc
```

Đây là các tên file mà cấu hình yêu cầu; cần tự kiểm tra đủ file trên máy. Dữ liệu thật không được sinh bởi script toy.

```powershell
uv run bcflow inspect data/OBS.Vietnam.1980-2014.nc
uv run bcflow validate configs/cnrm_obs_vietnam.yaml
uv run bcflow run configs/cnrm_obs_vietnam.yaml
```

Cấu hình chạy `precip`, `tmax`, `tmin`, hiệu chuẩn 1980–2004, kiểm định 2005–2014; kết quả nằm dưới `outputs/cnrm_obs_vietnam/`. Cấu hình khai báo đơn vị quan trắc vì file dùng nhãn `var_unit`.

Muốn thử một vùng nhỏ, sao chép cấu hình rồi thêm `region` vào phần `grid` hiện có:

```yaml
grid:
  # Giữ lại path, var_map và units của cấu hình gốc.
  region:
    lat: [20.0, 21.5]
    lon: [105.0, 107.0]
  block_size: 100
  workers: 1
  memory_fraction: 0.6
```

Đoạn này minh họa các trường cần sửa, không phải toàn bộ cấu hình. `region` là khung chữ nhật theo tọa độ, không phải cắt theo ranh giới lưu vực dạng polygon.

### Điều chỉnh CPU và RAM

- `grid.block_size`: số ô xử lý mỗi block; mặc định 500. Giảm khi thiếu RAM.
- `grid.workers: auto`: tự chọn số worker dựa trên CPU, RAM, số block và giới hạn cấu hình.
- `grid.workers: 1`: xử lý tuần tự; có thể hữu ích khi thử nghiệm hoặc máy ít RAM.
- `grid.memory_fraction`: phần RAM khả dụng dùng trong ước lượng; mặc định 0,6, không phải giới hạn cứng bảo đảm tránh hết bộ nhớ.

Các tùy chọn workers này áp dụng cho tham chiếu lưới. Nhiều workers hơn không luôn nhanh hơn.

Phương pháp đưa mô hình về lưới tham chiếu có `nearest`, `idw`, `bilinear`, `bicubic`, `conservative`, `patch`. Mặc định mưa dùng `conservative`, các biến khác dùng `bilinear`; chỉnh riêng qua `correction.<biến>.regrid`.

## 8. Đọc kết quả

Mỗi lần chạy tạo một thư mục riêng, không ghi đè lần trước:

```text
outputs/run_YYYYmmdd_HHMMSS/
  manifest.json
  config.json
  qa.json
  precip/
    scen.nc
    scen.csv
    adj.nc
    evaluation.csv
    hist_eval.nc
    hist_eval.csv
  tmax/
    ...
```

Cây trên minh họa một lần chạy xuất cả NetCDF và CSV. Thư mục gốc thực tế theo `output.path`; file cụ thể phụ thuộc định dạng, phương pháp và trạng thái từng biến.

| File | Nội dung / cách dùng |
|---|---|
| `manifest.json` | Điểm bắt đầu để xem trạng thái lần chạy và đường dẫn kết quả từng biến |
| `config.json` | Bản cấu hình thực tế với đường dẫn đã phân giải |
| `qa.json` | Kết quả kiểm tra có trạng thái `pass`, `warn`, `fail` và thông điệp |
| `<biến>/scen.nc` hoặc `scen.csv` | Chuỗi `model.sim` sau hiệu chỉnh |
| `<biến>/adj.nc` | Bộ tham số đã huấn luyện, nếu phương pháp trả về đối tượng adjustment |
| `<biến>/evaluation.csv` | Chỉ số đánh giá trước và sau hiệu chỉnh, nếu có |
| `<biến>/hist_eval.nc` hoặc `.csv` | Quan trắc, mô hình lịch sử thô và đã hiệu chỉnh: `obs`, `raw`, `corrected`, kèm nhãn `period` |

Kết quả lưới có chiều `(time, lat, lon)`, ô không có dữ liệu để NaN. CSV lưới ở dạng dài với tọa độ; dung lượng có thể lớn. `scen.csv` theo trạm có thời gian ở hàng và trạm ở cột.

Kiểm tra cả `manifest.json` và `qa.json`, kể cả khi đã có file kết quả. Một biến có thể lỗi trong khi biến khác hoàn tất. Trạng thái từng biến gồm `ok`, `failed`, `cancelled`, `skipped`.

## 9. Theo dõi và hủy tác vụ

CLI hiển thị tiến độ trên stderr. Phần trăm ô hoàn tất không tương đương phần trăm tổng thời gian vì còn quét dữ liệu, gộp và ghi file.

- Nhấn **Ctrl+C một lần** để yêu cầu dừng; phép tính hoặc batch đang chạy có thể phải kết thúc trước.
- Các biến đã hoàn tất được lưu. Các block của biến đang bị ngắt bị bỏ; biến chưa chạy được đánh dấu bỏ qua.
- Đây không phải cơ chế checkpoint để chạy tiếp từ block cũ.
- Nhấn **Ctrl+C lần hai** để buộc dừng có thể làm file đầu ra không hoàn chỉnh.

| Mã thoát của `run` | Ý nghĩa |
|---|---|
| `0` | Hoàn tất thành công |
| `1` | Hoàn tất nhưng có lỗi |
| `2` | Không thể khởi chạy |
| `130` | Đã hủy |

Với `validate`, mã `0` nghĩa là cấu hình hợp lệ, có thể kèm cảnh báo; mã `1` nghĩa là không hợp lệ.

## 10. Dùng trong script hoặc tích hợp ứng dụng

```powershell
uv run bcflow validate configs/example_toy.yaml --json
uv run bcflow inspect data/toy/model_hist_pr.nc --json
uv run bcflow methods --json
uv run bcflow run configs/example_toy.yaml --json
```

Với `run --json`, stdout là từng dòng sự kiện JSON; dòng cuối là `run_finished`. Các lệnh `validate`, `inspect`, `methods` với `--json` trả một tài liệu JSON. `schema` luôn xuất JSON Schema. Log thư viện và lỗi chẩn đoán đi ra stderr.

Ứng dụng cha có thể truyền `--cancel-file PATH`; khi file đó xuất hiện, CLI yêu cầu dừng tại checkpoint tiếp theo. Chọn đường dẫn mới cho từng tác vụ và bảo đảm file chưa tồn tại trước khi chạy.

## 11. Xử lý vấn đề thường gặp

| Hiện tượng | Cách kiểm tra / xử lý |
|---|---|
| Không tìm thấy `uv` | Kiểm tra đã cài uv và terminal nhận PATH mới |
| Môi trường Python không chạy trên Windows | Đặt `UV_PROJECT_ENVIRONMENT=.venv-windows` trong PowerShell rồi `uv sync --extra web` |
| Thiếu Streamlit | Chạy `uv sync --extra web` và khởi động bằng lệnh có `--extra web` |
| Cổng 8501 bị chiếm | Dùng `--server.port=8502`, mở đúng cổng mới |
| Không tìm thấy dữ liệu | Kiểm tra đường dẫn theo thư mục chứa cấu hình và ánh xạ `{var}` |
| Thiếu biến hoặc sai tên | Dùng `inspect`, đối chiếu `variables`, `model.var_map`, `grid.var_map` |
| Lỗi đơn vị, ví dụ `var_unit` | Xác minh đơn vị thực và khai báo `grid.units` hoặc `model.units` |
| Hiệu chuẩn và kiểm định chồng nhau | Chỉnh hai khoảng thành các giai đoạn tách biệt |
| Cảnh báo thiếu dữ liệu | Kiểm tra thời kỳ và ngày hợp lệ; dưới `min_coverage` vẫn có thể chạy nhưng cần xem QA |
| Thiếu RAM / chạy chậm | Thử vùng nhỏ, giảm `block_size`, đặt `workers: 1`; hạn chế CSV với lưới lớn |
| Nút Run không bật | Đợi validate thành công cho chính bản nháp hiện tại; validate lại sau mỗi lần sửa |
| Yêu cầu hủy nhưng chưa dừng | Chờ phép tính hoặc batch hiện tại và giai đoạn lưu kết quả hoàn tất |
| Có kết quả nhưng thiếu một biến | Xem trạng thái biến trong `manifest.json` và lỗi trong `qa.json` |

## 12. Tài liệu đọc tiếp và kiểm tra cho người phát triển

- [README dự án](README.md): thiết kế, CLI và giao thức JSON.
- [README giao diện web](app/README.md): vòng đời tác vụ, bản nháp và xem kết quả.
- [Cấu hình trạm mẫu](configs/example_toy.yaml), [cấu hình lưới mẫu](configs/example_grid.yaml), [CNRM + OBS.Vietnam](configs/cnrm_obs_vietnam.yaml).
- [Mục lục tài liệu công cụ AI](docs/README.md): tra cứu khi cần quy trình phối hợp AI; các hướng dẫn này không thay thế cách vận hành bcflow ở trên.
- [Schema cấu hình](src/bcflow/config_schema.py), [mặc định từng biến](src/bcflow/variables.py), [quy cách file xuất](src/bcflow/writers.py).

Kiểm thử dự án khi phát triển:

```powershell
uv run --extra web python -m pytest -q
```

Tài liệu này được đối chiếu với nguồn trong bản làm việc. Việc tổng hợp tài liệu không đồng nghĩa các lệnh cài đặt hoặc pipeline đã được chạy lại thành công trên máy.
