# BOT-CEO — Điều phối đa mảng (lane), bộ nhớ dùng chung và kinh phí AI

Tài liệu bổ sung cho [AGENTS-TUTORIAL.md](AGENTS-TUTORIAL.md) (v4.0) và [HERDR-CORE-BOT.md](HERDR-CORE-BOT.md), chuẩn hóa cho các dự án đa lane dùng **`agm` (`agmag` v0.4.0)**, **`hc` (`herdr-core`)**, **Understand-Anything (`.ua/`)**, **Ponytail**, **Antigravity (`agy`)** và **GitHub Copilot**.

> **Thứ tự ưu tiên:** `AGENTS.md` / `CLAUDE.md` của dự án **>** [AGENTS-TUTORIAL.md](AGENTS-TUTORIAL.md) **>** `BOT-CEO.md` **>** [HERDR-CORE-BOT.md](HERDR-CORE-BOT.md).
>
> **Quy tắc cốt lõi của Claude BOT-CEO:**
> - **1 lane = 1 BOT-CEO** kiêm Kiến trúc sư của lane đó (chat trực tiếp trong HERDR kèm `agm manage <tên-terminal>`).
> - **Ponytail: `lite` hoặc `off` (cấm `ultra` mặc định)** để không tự thu hẹp không gian thiết kế giải pháp và đánh giá các phương án.
> - **Shared Memory:** Claude CEO là vai **duy nhất** có quyền **Approve, publish, update** `.agents/memory/published/` và chạy batch incremental `/understand` trên `main`.
> - **Kinh phí AI:** Kiểm `agm quota --refresh` và [Go Console](https://opencode.ai/console/go) trước khi giao/review/nâng bậc ([quy tắc ngân sách chung](AGENTS-TUTORIAL.md#311-điều-phối-kinh-phí-ai)). Nhiều lane dùng chung Go chỉ định 1 CEO đầu mối để không cấp trùng.

---

## 1. Vì sao tách nhiều BOT-CEO & Nguyên tắc cô lập

Khi dự án lớn có nhiều mảng nghiệp vụ, gom hết vào 1 phiên điều phối dễ gây phình ngữ cảnh và nghẽn hàng đợi. CEO chủ động rotate ở 50%, hoàn tất bàn giao trong khoảng 50–55%; việc dài chọn mốc sớm hơn theo [chính sách ngữ cảnh](docs/CONTEXT-ROTATION.md). Mỗi mảng có **1 BOT-CEO độc lập** (khuyến nghị ≤ 4–5 lane), đăng ký nhánh vào sổ chung **`BRANCHES.md`** để **1 phiên `copilot-git` duy nhất** merge tuần tự.

**Ba nguyên tắc cô lập:**
1. **Không truyền lịch sử hội thoại giữa lane/agent:** BOT-CEO lane A chỉ đọc `BRANCHES.md`, `.ua/` và `.agents/memory/published/`; mọi bàn giao đi qua báo cáo ngắn chuẩn hóa.
2. **Cô lập cây làm việc:** Checkout chính (`main`) chỉ đọc + ghi `BRANCHES.md` qua `agm branch set` (chỉ `copilot-git` merge vào `main`; chỉ CEO publish `.agents/memory/published/` và `.ua/`). Code chạy trong worktree tách rời (`.wt/<lane>--<slug>`).
3. **Gom điểm nóng (`touches_hotspot`):** Các việc chạm cùng nhóm tệp dồn vào cùng 1 lane và xếp nối tiếp bằng `depends_on`.

---

## 2. Phân vai trong chế độ BOT-CEO

Chi tiết đầy đủ về model, phân quyền bộ nhớ và Ponytail nằm tại [AGENTS-TUTORIAL.md](AGENTS-TUTORIAL.md). Chọn skill theo [giai đoạn, vai và loại việc](docs/SKILL-ROUTING.md), không dùng Ponytail thay workflow. Tóm tắt vận hành theo lane:

| Vai trò | Công cụ / Model | Ponytail | Quyền bộ nhớ & Phạm vi |
|---|---|---|---|
| **BOT-CEO (theo lane)** | Claude Code (`claude-opus-5-5` / `claude-fable-5-2`) | `lite` / `off` | Điều phối, phân bổ ngân sách, viết Brief, giữ `wip`/`ready`; **Approve, publish, update** `.agents/memory/published/` & batch `/understand` |
| **Antigravity (`agy`)** | Opus High (Co-Arch / Senior Reviewer), Argon (harness), triển khai worktree | Thiết kế `off` / `lite`; triển khai/review `full` | Triển khai chính xác tối thiểu; **Propose, review, flag conflicts** vào `.agents/memory/inbox/` (không tự duyệt code của mình) |
| **Codex (Dev / Reviewer)** | `openai/gpt-6.1-sol` | `full` | Engineering khó / patch nhỏ / review sâu correctness, security, test, perf; **Propose, write task findings** |
| **Workers & Reviewer mặc định** | Flash Go (`worker-fast/mid`, `explore`), Pro Go (`reviewer`, `worker-strong`), Audit `script/CI` | Quy tắc rút gọn | Làm đúng `files_allowed`, **Write task logs & implementation findings** vào `.agents/log/<ID>.md` |
| **Docs & Báo cáo cuối** | `agy`: `gemini-3.8-flash-medium` / `hydrogen-low` (`hydrogen-medium` chỉ cho docs dài) | — | Cập nhật docs & `.agents/reports/<ID>.md` sau Reviewer, trước `copilot-git` |
| **Bot GIT** | GitHub Copilot CLI (`copilot-git`, đúng 1 phiên) | — | Merge tuần tự đúng `head` SHA, chạy Audit hậu merge, chặn nhánh non-CEO sửa `published/`/`.ua/`, dọn worktree |

*Lưu ý:* [NVIDIA nền](docs/NVIDIA-BACKGROUND.md) chỉ đọc, không chặn merge, tối đa 1 việc thủ công trên cả nhóm alias.

---

## 3. Giao thức `BRANCHES.md` và khóa ghi song song

Mỗi nhánh ghi 1 khối `### <lane>/<slug>` chuẩn khóa-giá trị:

```markdown
### tap-hoa/low-stock
- status: ready
- lane: ceo-tap-hoa
- tier: vừa
- head: 9f8e7d6c5b4a      # SHA cuối (đã gồm commit docs nếu có); copilot-git merge ĐÚNG SHA này
- base: 1a2b3c4d5e6f
- depends_on: -
- migration: no
- touches_hotspot: -
- summary: Cảnh báo hàng sắp hết khi tồn dưới ngưỡng cấu hình
- verify: <lệnh test/lint thực tế + số đếm>
- review: .agents/reviews/tap-hoa--low-stock-1.md@3c2b1a0f9e8d opencode-go/deepseek-v4-pro, 0 bắt buộc sửa
- docs: gemini-3.8-flash-medium (đã cập nhật docs/inventory.md) | không đổi
- registered: 2026-11-22T08:40+07:00
- git: -
```

**Trạng thái:** `wip` (CEO giữ chỗ) → `blocked` (chờ quyết định / `depends_on`) → `ready` (qua Audit + Reviewer + Docs + rebase + `head` SHA cố định; cấm commit thêm) → `merging` (`copilot-git`) → `merged` / `deployed` (hoặc `needs-rebase` / `failed` trả về lane).

**Ghi `BRANCHES.md` chỉ qua `agm`** (`agm 0.4.0`): lệnh tự lấy khóa, sửa đúng một mục, ghi nguyên tử rồi nhả khóa. Không tự khóa bằng tay.

```bash
# Giữ chỗ / cập nhật một mục (BRANCHES.md phải có sẵn; dùng đường dẫn tuyệt đối tới checkout chính)
agm branch set <lane>/<slug> status=wip lane=<lane> touches_hotspot=- --file /abs/BRANCHES.md
# Chốt ready: --worktree để agm kiểm worktree sạch, HEAD khớp head và bằng chứng review
agm branch set <lane>/<slug> status=ready head=$HEAD_SHA "verify=..." "review=.agents/reviews/<ID>-<n>.md@$REVIEWED_SHA <model>, <số blocker>" --file /abs/BRANCHES.md --worktree /abs/.wt/<lane>--<slug>
# Đọc một mục
agm branch get <lane>/<slug> --file /abs/BRANCHES.md
```

- Agent có MCP server `agm` dùng `agm_branch_update` (`branches_file` tuyệt đối) / `agm_branch_get`.
- Khóa là `.agents/.runtime/locks/branches` cạnh `BRANCHES.md` (đã `.gitignore`); `agm` giữ vài mili-giây, chờ tối đa 30 giây và **thu hồi khóa cũ hơn 60 giây**, nên **cấm `mkdir`/`rmdir` khóa tay**. Terminal vai `worker` bị từ chối ghi.
- **Guard `ready` (agm ≥ 0.4.1):** agm kiểm `head` là SHA 12–40 ký tự hex, worktree sạch (bỏ qua `.agents/tasks/`, `.agents/.runtime/`), HEAD thật khớp `head`, và `review=.agents/reviews/<ID>-<n>.md@<sha>` (token đầu; phía sau được ghi chú tự do): tệp review phải đã commit ở HEAD, nhắc 7 ký tự đầu của `<sha>`, và từ `<sha>` tới HEAD không đổi tệp nào ngoài `.agents/`. `$REVIEWED_SHA` là commit Reviewer đã đọc (tệp review ghi nó ở dòng `Đối tượng: HEAD ...`); commit tệp review/log sau đó vẫn hợp lệ, còn sửa mã sau review thì bị từ chối và phải review lại. Guard **không** đọc verdict và không kiểm Audit: CEO vẫn tự xác nhận review ĐẠT và `verify=` xanh. Đổi `review` của mục đã `ready` cũng chạy lại guard.

---

## 4. Vòng đời đầu việc trong BOT-CEO

### 4.1. Phân loại trước khi chạy (Tránh nặng nề cho việc nhỏ)

- **Bugfix / task nhỏ ≤ 3 tệp đã rõ yêu cầu:** **Bỏ qua** khung 6 bước bên dưới; kiểm `.ua/` + `.agents/memory/published/` rồi viết thẳng `.agents/tasks/<ID>.md` và giao thợ.
- **Epic / tính năng mới / thay đổi kiến trúc lớn (khung Lauren Tan: `Goal first · Process second · Then routines`):**
  1. `goal.first`: làm rõ mục tiêu và giá trị (`serves: revenue | cost`); hỏi tối đa **1 câu/lượt** về quyết định ẩn.
  2. `process.dump` + `hold`: gom `steps`, `watch_outs`, `tools`, `check_work`, `info_from`, `feedback_from`; `/restate` và **dừng chờ xác nhận** trước khi `agm spawn`.
  3. `how.i.think`: đi qua **3 ví dụ thực tế (gồm ca lỗi)**, chốt nguồn thắng khi `conflict`, định nghĩa `done = saved + confirmed` (không coi "đã giao thợ" là xong; không tạo task trùng cho mục đang `waiting`).
  4. `review`: chốt phân bổ ngân sách và xuất `{ goal, process, rules, routine, budget, next_step }` kèm 1 câu xác nhận cuối.

### 4.2. Luồng thực thi 7 bước của BOT-CEO

1. **Khởi động & quản lý ngữ cảnh:** Mở terminal `ceo-<lane>` (≤ 32 ký tự `a-z 0-9 - _`); người dùng bật `agm manage ceo-<lane>` một lần. CEO theo dõi số đo ngữ cảnh tại các checkpoint, tự gọi `agm rotate ceo-<lane>` ở 50% và không bắt đầu pha lớn khi đã 55%. Dự báo việc dài thì chọn sớm, thường 40% (35–45%) hoặc thấp hơn nếu cần; ghi mốc/lý do và bàn giao quyết định, quyền đã duyệt, task đang chạy theo [Context Rotation](docs/CONTEXT-ROTATION.md), không chờ fallback chung 70%.
2. **Đọc ngữ cảnh tối giản:** Truy vấn **Understand-Anything (`.ua/`)** (`/understand-chat`, `/understand-diff`) + đọc `.agents/memory/published/` và `BRANCHES.md` (kiểm điểm nóng). Không nhồi toàn bộ `.ua/knowledge-graph.json` cho worker.
3. **Giữ chỗ & giao việc trực tiếp:** Ghi `status: wip` bằng `agm branch set` (hoặc `agm_branch_update`). Viết `.agents/tasks/<ID>.md` (mục tiêu, `files_allowed`, `tier`, `phase`, `work_type`, `ponytail`, `skills`, `skill_refs`, lý do bổ sung và lệnh kiểm; nguồn/model đã kiểm qua `agm quota --refresh`). Chọn một workflow + một domain theo [Skill Routing](docs/SKILL-ROUTING.md), giữ verification và các skill bắt buộc, kiểm khả dụng đúng runtime rồi gọi `dispatch.sh <ID>` (`agm spawn`). Nhiều đầu việc độc lập (tệp/module/test tách biệt) thì khởi động song song trong cùng một đợt, trong mức `max_workers` (chỉ đếm `--role worker`) và ngân sách đã duyệt.
4. **Theo dõi & chặn vòng lặp:** Nhận tín hiệu ngắn qua `agm send`; câu hỏi cần người dùng quyết đi qua `agm send user --type HỎI`. Khi vòng review trả `CHƯA ĐẠT`, chỉ đính kèm mục `Previous blockers` vào Brief (không truyền lịch sử chat). **Tối đa 2 vòng sửa** — lần 2 vẫn fail thì CEO trực tiếp chẩn đoán/leo thang.
5. **Cổng chất lượng:** `Audit (script/CI + ESLint --max-warnings 0)` → `Reviewer (1 tầng độc lập, không tự review)` → `Docs & Report (agy flash-medium / hydrogen-low nếu có thay đổi)`.
6. **Rebase & bàn giao `copilot-git`:** Rebase lên `main`, chạy lại kiểm chứng, lấy `HEAD_SHA=$(git rev-parse --short=12 HEAD)`, đặt `status: ready` bằng `agm branch set ... status=ready head=$HEAD_SHA verify=... review=.agents/reviews/<ID>-<n>.md@<sha đã review> docs=... registered=... --worktree <worktree>` (guard kiểm git và bằng chứng review, xem §3) và nhắn `agm send copilot-git --type GIAO --task <ID> "..."`.
7. **Chốt Shared Memory & Đồ thị (sau merge):**
   - Duyệt `.agents/memory/inbox/` (proposals từ Codex/Agy và conflict flags từ Agy) → gộp tri thức dài hạn vào `.agents/memory/published/`, **xóa tệp đã xử lý trong `inbox/`**, cập nhật `HANDOFF.md` (≤ 30 dòng).
   - Khi có thay đổi cấu trúc/module lớn, chạy batch incremental `/understand` trên `main` và commit `.ua/`.
   - Cuối sprint: chạy `/ponytail-debt` để chốt các `shortcut:` tồn đọng vào `published/gotchas.md`.

---

## 5. Giới hạn & Điều BOT-CEO không bao giờ làm

| Tham số | Quy chuẩn |
|---|---|
| Số BOT-CEO đồng thời | ≤ 4–5 lane (`agm manage` từng lane) |
| Tổng thợ đồng thời / Trần token | `[policy].max_workers` + `agm budget set task:<ID>` |
| Migration `wip`/`ready` mỗi lane | Tối đa 1 |
| Số phiên `copilot-git` | **Đúng 1** (merge tuần tự) |
| Quyền dừng Claude / Codex | **Chỉ người dùng** (TTY + xác nhận `y`) |

**Cấm tuyệt đối:** đọc sâu mã lane khác không chung hotspot; sửa `BRANCHES.md` bằng tay hoặc tự `mkdir` khóa (chỉ qua `agm branch set` / `agm_branch_update`); đặt `ready` thiếu Audit/Reviewer; tự merge/push/deploy; tự bật Extra Usage hoặc vượt trần chi phí; bật `ponytail ultra` mặc định; cho phép non-CEO ghi `.agents/memory/published/`.

**Bài học vận hành đã kiểm chứng (2026-11-24):** hai nhánh song song cùng sửa một module phải được merge thử cả hai đầu và chạy đủ test trước khi merge; số test kỳ vọng tính bằng phép cộng; đặc tả sửa cho đường thoát đồng thời cần được phản biện trước khi giao vì thợ làm đúng từng chữ; thợ `opencode run` không báo được khi terminal CEO đang bận nên CEO theo dõi trạng thái `hc`. Chi tiết: [docs/WINDOWS-AGENT-RUNTIME.md](docs/WINDOWS-AGENT-RUNTIME.md).

---

## 6. Mẫu prompt khởi động BOT-CEO

```text
role := Bạn là Claude BOT-CEO lane <tên-lane> (terminal: ceo-<tên-lane>). Ponytail: off/lite (không dùng ultra).
Đọc AGENTS.md, BOT-CEO.md, .agents/memory/published/ và BRANCHES.md. Dùng Understand-Anything (.ua/) trước khi đọc mã. Theo docs/CONTEXT-ROTATION.md: khi đã managed, tự yêu cầu rotate ở 50%, dùng tối đa 55% để bàn giao an toàn; việc dài chọn khoảng 40% hoặc thấp hơn khi cần. Không tự kill phiên hoặc giao lại task còn chạy.

goal := <MỤC TIÊU CUỐI CÙNG>
input :=
- ...

Quy tắc:
1. Nếu là bugfix/task nhỏ <= 3 tệp đã rõ: viết thẳng .agents/tasks/<ID>.md rồi chờ tôi duyệt "chạy".
2. Nếu là Epic/tính năng lớn: làm theo mục 4.1 (goal.first -> process.dump + hold -> 3 ví dụ gồm ca lỗi -> xuất { goal, process, rules, routine, budget, next_step }, hỏi tối đa 1 câu/lượt). KHÔNG agm spawn trước khi tôi chốt.
3. Khi chạy: kiểm `agm quota --refresh`, giữ chỗ `wip` bằng `agm branch set`, giao báo cáo chuẩn ngắn (không truyền lịch sử hội thoại), tối đa 2 vòng fix. Chọn skill theo docs/SKILL-ROUTING.md; ghi phase/work_type, skills và skill_refs trong Brief, không bỏ verification vì trần số skill. Sau merge duyệt .agents/memory/inbox/ -> publish vào .agents/memory/published/.
```
