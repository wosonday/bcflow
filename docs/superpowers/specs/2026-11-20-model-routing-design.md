# Thiết kế A — NVIDIA làm việc nền, DeepSeek Go làm việc tương tác

## Quyết định và mục tiêu

- Người dùng chọn phương án A: tận dụng model NVIDIA chậm cho công việc nền, không kéo dài luồng code → Audit → Reviewer → docs → tích hợp.
- Bổ sung theo yêu cầu: đã mở OpenCode Go; ưu tiên các model DeepSeek phù hợp để giảm tải Codex/Claude, thay vì chỉ nói chung chung “dùng Go”.
- Đây là thay đổi nhỏ trong bộ tài liệu harness dùng chung. Chưa triển khai scheduler, plugin hoặc thay cấu hình toàn máy.
- Trạng thái: người dùng đã duyệt thiết kế A và yêu cầu viết lint kỹ. Triển khai cập nhật harness và lint tự động kèm test; không thay cấu hình toàn máy.

## Các phương án

| Phương án | Đánh đổi | Quyết định |
|---|---|---|
| A. NVIDIA chỉ nhận việc nền; DeepSeek Go đảm nhiệm công việc tương tác phù hợp | Tận dụng được NVIDIA mà không phụ thuộc độ trễ của chúng; chưa giảm tải Go bằng việc giao code cho NVIDIA | Người dùng đã chọn |
| B. NVIDIA nhận cả code và review thường | Có thể giảm tải Go hơn, nhưng thêm rủi ro tool calling và chậm đường găng; cần kiểm chứng lớn hơn | Không triển khai trong đợt này |

## Bằng chứng và giới hạn

Danh mục OpenCode của workspace đã xác nhận các ID sau; việc có trong danh mục không chứng minh đã gọi API thành công hoặc đã đạt chất lượng theo vai:

- `opencode-go/deepseek-v4.1-flash`
- `opencode-go/deepseek-v4-pro`
- `nvidia-kimi/moonshotai/kimi-k3.1-pro`
- `nvidia-mistral/mistralai/mistral-large-4-1t-a52b`
- `nvidia-nemotron/nvidia/nemotron-3-ultra-550b-a55b`

Slug cũ `opencode/deepseek-v4.1-flash-free` không xuất hiện trong kết quả kiểm danh mục. Không dùng nó làm mặc định hay mặc định DeepSeek hiện miễn phí. Không thay bằng `opencode/deepseek-*` vì đó là nguồn khác với `opencode-go/*`.

Opus chưa đưa ra phản biện: API Anthropic thiếu credit; Opus qua `agy` hết quota. Không ghi thiết kế này là đã được Opus duyệt.

## Phân vai đề xuất

| Công việc | Nguồn ưu tiên | Điều kiện |
|---|---|---|
| Lint, test, build, quét diff, GitNexus index | Script/CI, không LLM | Không dùng Go hoặc NVIDIA để làm lại công việc xác định |
| `worker-fast`, `worker-mid`; tìm mã/tóm tắt cần trả lời sớm | DeepSeek V4.1 Flash trên Go | Trong mức CEO cấp; thử gọi công cụ theo vai trước khi dùng thật; chỉ gọi LLM khi cần |
| Reviewer nhánh thường; `worker-strong` ở mức đã kiểm chứng | DeepSeek V4 Pro trên Go | Review độc lập, một tầng; không mặc định đủ năng lực cho mọi ca khó |
| Engineering thực sự khó, bảo mật/dữ liệu/migration, phân xử | Giữ thang leo Codex / Opus / Claude theo harness hiện có | Không hạ cổng chất lượng để tiết kiệm hoặc vì thiếu quota |
| Tổng hợp mã/tài liệu đã chọn cho task tương lai | NVIDIA Kimi — ứng viên thử nghiệm | Chỉ đọc; không thay `explore` tương tác hoặc giao code |
| Soát tài liệu, đề xuất chỉnh diễn đạt/tính nhất quán | NVIDIA Mistral — ứng viên thử nghiệm | Trả đề xuất; vai Docs hiện có vẫn chịu trách nhiệm sửa tài liệu chính thức |
| Tổng hợp log, tìm lỗi lặp, đề xuất cải thiện harness sau phiên | NVIDIA Nemotron — ứng viên thử nghiệm | Không thay Audit hay Gemini Argon quyết định thay đổi harness |

Phân vai từng model NVIDIA là giả thuyết thử nghiệm, không phải kết luận benchmark. Có thể đổi vai sau khi đo trên cùng loại đầu việc.

Nếu Pro đã viết code, Reviewer dùng phiên độc lập với model khác họ đã duyệt và đủ năng lực. Reviewer không đọc lịch sử suy luận của thợ. Không thêm NVIDIA thành tầng review thứ hai.

## Quy tắc vận hành

1. **Tách năng lực và độ trễ:** giữ `tier: nhanh | vừa | mạnh` theo tương thích hiện tại; ghi thêm yêu cầu tương tác hay nền trong brief. “Nhanh” là tên bậc cũ, không là số đo tốc độ. Chậm không đồng nghĩa yếu. Các thông tin bổ sung trong brief không phải khóa cấu hình mới mà `agm` tự hiểu.
2. **Go đã mở không có nghĩa quota vô hạn:** kiểm Console cho 5 giờ/tuần/tháng, giữ phần review/sửa, Extra Usage tắt trừ khi được duyệt. Flash và Pro cùng rút hạn mức Go; đổi giữa chúng không giải quyết cạn quota. Hết/không rõ quota thì dùng nguồn đã duyệt đủ năng lực hoặc `blocked`; không tự chuyển sang API trả theo mức dùng.
3. **Qualification theo vai:** Flash thử một task sửa một tệp → lint/test → nhật ký → commit; Pro thử review một diff có lỗi đã biết và đối chiếu bằng chứng. NVIDIA thử trước một mẫu chỉ đọc cho từng vai, ghi thời gian, độ đúng và khả năng trích dẫn vị trí; chỉ mở rộng sau khi đạt. Không chạy thử trong đợt sửa tài liệu nếu chưa kiểm được ngân sách/quyền dữ liệu.
4. **NVIDIA là cloud:** không gửi khóa, dữ liệu khách hoặc mã nhạy cảm nếu chưa được người dùng duyệt. Không suy ra miễn phí từ metadata giá trống; xác minh quyền sử dụng, phí và quota trước khi giao.
5. **Không fan-out:** ban đầu tối đa một tác vụ NVIDIA cùng lúc trên cả ba alias, do CEO điều phối thủ công. Đây là giới hạn thận trọng, không là khẳng định ba alias chắc chắn chung quota. Không dùng alias khác để né `429`.
6. **Deadline rõ ràng:** mỗi brief nền có hạn hoàn thành; đề xuất khởi đầu 15 phút cho pilot. Quá hạn thì ngừng chờ và ghi hết hạn, không mở task trùng. Người điều phối kiểm trạng thái task cũ trước khi giao lại; không tự ngắt phiên Codex/Claude. Chưa có wrapper được kiểm thì không mô tả deadline là cơ chế tự động.
7. **Không chồng retry:** `429`/hết quota thì dừng giao thêm vào phạm vi bị ảnh hưởng, tuân thủ `Retry-After` hoặc thời điểm reset; chưa rõ thì chờ CEO kiểm. Với timeout, không bọc thêm vòng retry bên ngoài retry sẵn có. Timeout HTTP không phải deadline toàn task.
8. **Đầu ra có nguồn và phiên bản:** ghi task ID, SHA đầu vào hoặc dấu vết snapshot nếu không có Git, danh sách tài liệu/log và thời điểm lấy. Kết quả muộn hoặc lệch SHA chỉ là tham khảo; phải đối chiếu lại trước khi dùng, không tự áp vào code/docs đã đổi.
9. **Không tranh quyền ghi:** agent NVIDIA chỉ đọc, trả đề xuất cho điều phối; không sửa code, docs chính thức, trạng thái nhánh hoặc log chung của phiên khác. Người đúng vai lưu kết quả cần giữ vào hồ sơ điều phối hiện có.
10. **Không chặn đường găng:** không đợi NVIDIA trước merge chỉ để đủ một báo cáo phụ; bằng chứng rủi ro nghiêm trọng nếu phát hiện vẫn phải báo và xử lý theo quy trình an toàn hiện có.

## Phạm vi sửa sau khi duyệt

- `AGENTS-TUTORIAL.md`: cập nhật phân vai, nguồn model và thang leo; bỏ mặc định DeepSeek “free” đã không còn được xác nhận.
- `BOT-CEO.md`: thêm chính sách cấp việc nền và phân bổ DeepSeek Go, không thay luồng tích hợp.
- `HERDR-CORE-BOT.md`: cập nhật hướng dẫn template/brief; phân biệt chính sách thủ công với khả năng thực tế của `agm`.
- `docs/OPENCODE-WORKERS-REVIEWER.md` và bản tiếng Anh: bảng routing cụ thể, qualification, quyền và giới hạn.
- `docs/README.md` và bản tiếng Anh: tóm tắt DeepSeek Go + NVIDIA, dẫn tới hướng dẫn chi tiết.
- Thêm hướng dẫn NVIDIA tiếng Việt/Anh nếu phần chi tiết cần tách để tránh lặp.
- Đồng bộ các dòng phân vai cũ trong hướng dẫn Codex/Claude Việt/Anh nếu còn mâu thuẫn với routing mới.
- Theo yêu cầu bổ sung: thêm lint Python thuần thư viện chuẩn và test hồi quy cho liên kết Markdown, cấu trúc tài liệu, ID model và bảng routing Việt/Anh. Lint không thay review ngữ nghĩa hay benchmark API.

Không sửa cấu hình OpenCode/`agm`/HERDR toàn máy, credential, model mặc định, mã dự án khác hoặc ngân sách tài khoản. Không dựng scheduler mới.

## Tiêu chí nghiệm thu

- Các tài liệu dùng thống nhất đúng ID Go đã kiểm, không đánh đồng Go với API Personal hay nguồn miễn phí.
- NVIDIA không trở thành worker/reviewer mặc định hoặc cổng bắt buộc trước tích hợp.
- Không làm mất yêu cầu lint/test, độc lập Reviewer, quyền dữ liệu và giới hạn chi phí.
- Không khẳng định timeout, cooldown, xếp hàng hoặc routing mới đã được `agm` thực thi tự động.
- Link nội bộ hợp lệ; bản Việt/Anh thống nhất các quy tắc mới; thay đổi chỉ nằm trong phạm vi trên.
- Báo cáo bàn giao phân biệt kiểm tài liệu với benchmark model; không ghi có benchmark hay Opus duyệt khi chưa thực hiện.
