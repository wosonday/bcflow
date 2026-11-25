# Kế hoạch cập nhật routing và lint harness

Thiết kế: [NVIDIA nền + DeepSeek Go](../specs/2026-11-20-model-routing-design.md), đã được người dùng duyệt.

Skill `superpowers:writing-plans` không khả dụng trong phiên; kế hoạch được ghi trực tiếp tại đây.

1. Cập nhật harness chính và bảng routing Việt/Anh: Flash cho worker-fast/mid, Pro cho review/strong phù hợp; script/CI cho việc xác định.
   - Kiểm: ID chính xác, bảng Việt/Anh khớp; không còn mặc định DeepSeek miễn phí chưa xác nhận.
2. Thêm hướng dẫn NVIDIA Việt/Anh, ràng buộc cloud, quota, deadline thủ công, snapshot và đầu ra muộn; đồng bộ các hướng dẫn vai đang mâu thuẫn.
   - Kiểm: NVIDIA chỉ đọc, không chặn merge, không giả định scheduler mới đã hoạt động.
3. Viết lint không phụ thuộc gói ngoài và bộ test ca tốt/xấu.
   - Kiểm: link/anchor nội bộ, UTF-8, Markdown cơ bản, model ID, bảng routing và các mục an toàn; lỗi có tệp:dòng, mã lỗi, exit code khác 0.
4. Chạy test, lint toàn bộ và kiểm tra lại thủ công.
   - Kiểm: test dương/âm đạt; lint 0 lỗi; ghi rõ chưa benchmark model và chưa có phản biện Opus.

Không commit: thư mục hiện tại không phải Git repo. Không gọi API model có tính phí để thử khi chưa xác minh ngân sách.

## Kết quả thực hiện

- Đã cập nhật routing v3.6 trong harness chính, BOT-CEO/HERDR và các hướng dẫn vai Việt/Anh; thêm hướng dẫn NVIDIA và lint hai ngôn ngữ.
- Đã sửa mẫu guard Bash/CI: Git lỗi/base sai không báo xanh; kiểm thêm MJS/CJS/MTS/CTS, nhận config lint trong thư mục con và bỏ qua header diff để tránh nhầm tên tệp.
- Đã thêm ba module Python và ba tệp test. Test shell trích chính mẫu tài liệu, chạy Git fixture độc lập bằng Git Bash trên Windows, không sửa cấu hình Git người dùng.
- Lượt kiểm cuối: 85 test đạt, 0 lỗi, 0 skip; lint 23 tài liệu đạt 0 lỗi; 6 tệp Python parse AST thành công.
- Ruff chưa cài trong môi trường; không cài thêm phụ thuộc và không ghi đã chạy Ruff/ESLint ứng dụng. AST/test/guard fixture là các kiểm thực tế ở đây.
- Chưa benchmark API DeepSeek/NVIDIA, chưa có phản biện Opus; không thay model mặc định, credential hoặc cấu hình toàn máy. Giới hạn NVIDIA mới vẫn là chính sách CEO thủ công, không là scheduler tự động.
