# Chỉ dẫn làm việc với đội agent (phiên bản 4.0: Understand-Anything + Shared Memory + Ponytail + DeepSeek Go + Claude + Codex + Agy + Copilot + agm)

> **Thứ tự ưu tiên (cao xuống thấp):** `AGENTS.md` / `CLAUDE.md` của dự án **>** `AGENTS-TUTORIAL.md` (quy tắc cứng, quyền & cổng) **>** [BOT-CEO.md](BOT-CEO.md) (đa lane & `BRANCHES.md`) **>** [HERDR-CORE-BOT.md](HERDR-CORE-BOT.md) (hạ tầng `agm`/`hc`). Chi tiết theo từng model nằm ở [docs/README.md](docs/README.md).
>
> **Cập nhật 4.0:** (1) **Bỏ GitNexus**, chuyển sang **[Understand-Anything](https://github.com/Egonex-AI/Understand-Anything)** lưu Knowledge Graph tại `.ua/` dùng chung cho mọi agent; (2) **Chuẩn hóa bộ nhớ dùng chung có phân quyền** (`.agents/memory/published/` & `inbox/`), **hạn chế truyền lịch sử hội thoại** giữa các agent bằng báo cáo ngắn chuẩn hóa; (3) Áp dụng **[Ponytail](https://github.com/DietrichGebert/ponytail)** phân tầng theo vai (cấm `ultra` mặc định); (4) Giữ routing DeepSeek Go (`opencode-go/deepseek-v4.1-flash` cho fast/mid/explore; `opencode-go/deepseek-v4-pro` cho reviewer/strong sau qualification) và `agm 0.4.0` (đọc hạn mức Go qua `agm quota --refresh`; ghi `BRANCHES.md` có khóa qua `agm branch`; MCP server `agm mcp` 7 tool cho quản lý/duyệt — xem [docs/AGM-0.4.0-GUIDE.md](docs/AGM-0.4.0-GUIDE.md)).

---

## 1. Kiến trúc & Vai trò

```
     HERDR / herdr-core (hc) + agm (daemon, TUI, sổ sách, quota, policy)
                                      │
          Understand-Anything (.ua/)  +  Shared Memory (.agents/memory/)
                                      │
                 CLAUDE CEO / ARCHITECT (Ponytail: lite / off)
                 (điều phối ngân sách, chốt kế hoạch, Approve/Publish memory)
                                      │
       ┌──────────────────────────────┼──────────────────────────────┐
       ▼                              ▼                              ▼
  Antigravity (Agy)            Codex (Dev / Reviewer)        OpenCode Workers
  - Ponytail: full             - Ponytail: full              - Ponytail: rút gọn
  - Triển khai / Co-Arch       - Engineering khó / Patch     - Fast/Mid: Flash Go
  - Propose, review,           - Reviewer (độc lập)          - Strong: Pro Go
    flag conflicts             - Propose, write findings     - Audit: script/CI
  - Docs/Report nhẹ                                          - Write task logs
       └──────────────────────────────┼──────────────────────────────┘
                                      ▼
           AUDIT (script/CI + ESLint/clippy) → REVIEWER (1 tầng, không tự duyệt)
                                      ▼
           DOCS & REPORT (agy flash-medium / hydrogen-low, nếu có thay đổi)
                                      ▼
           GIT INTEGRATION (copilot-git, đúng SHA, chặn sửa trái phép published/ & .ua/)
                                      ▼
           CEO: Approve & Publish .agents/memory/published/ · Batch incremental /understand
```

### 1.1. Bảng vai trò & giới hạn quyền

| Cấp | Vai trò | Model / Công cụ | Quyền cốt lõi | Không được làm |
|---|---|---|---|---|
| 1 | **Claude CEO / Kiến trúc sư** | `claude-opus-5-5` / `claude-fable-5-2` (Claude Code + `agm manage`) | Lập kế hoạch, phân bổ kinh phí, chia nhánh (< ~400 dòng diff), giữ `wip`/`ready` trên `BRANCHES.md` (qua `agm branch set`), **Approve / Publish / Update** `.agents/memory/published/` & chạy batch `/understand` | Sửa code thay thợ, tự merge/push/deploy, tự bật Extra Usage, bật `ponytail ultra` mặc định |
| 1–2 | **Antigravity (`agy`)** | `agy`: Opus High (Co-Arch / Senior Reviewer giữ `conversation_id`), Gemini Argon (cố vấn & tối ưu harness), triển khai trong worktree | Triển khai chính xác & tối thiểu theo kế hoạch; **Propose, review, flag conflicts** vào `.agents/memory/inbox/` & `.agents/reviews/` | Tự duyệt code do chính `agy` viết; sửa trực tiếp `.agents/memory/published/` |
| 2 | **Codex (Developer & Reviewer)** | `openai/gpt-6.1-sol` (Codex CLI / OpenCode) | Engineering khó / surgical patch / fix sau 2 lần fail; Review tập trung correctness, security, testing, perf, complexity; **Propose, write task findings** | Ghi trực tiếp `.agents/memory/published/` hay `.ua/`; viết báo cáo hành chính |
| 2 | **Reviewer mặc định** | **`agy` trước** (quyết định người dùng 2026-11-24): `gemini-4-hydrogen-high` (`--mode plan --sandbox`, đầu vào là tệp `REVIEW-INPUT.md`) → `gemini-4-neon-high` cho việc rủi ro cao, chẩn đoán khó hoặc khi Hydrogen viết code → Claude Opus chỉ khi Neon cũng không giải được. Dự phòng: OpenCode `reviewer` = `opencode-go/deepseek-v4-pro` (sau qualification; khác họ nếu Pro viết code) | Phiên mới độc lập, đọc diff + task + log + `.ua/`; chỉ ghi `.agents/reviews/` | Sửa mã sản phẩm; tự duyệt cùng họ model đã viết code (ngoại lệ người dùng chấp nhận: Hydrogen ↔ Neon khác bậc cho code thường; thay đổi rủi ro cao vẫn cần thêm một review khác họ) |
| 2 | **Bot GIT** | GitHub Copilot CLI (`copilot-git`, **đúng 1 phiên**) | Merge tuần tự đúng `head` SHA trên `main`, kiểm hậu merge, revert, dọn worktree/nhánh đã merge | Sửa logic tính năng; merge nhánh sửa trái phép `published/` hoặc `.ua/` |
| 3 | **Docs & Báo cáo cuối** | **Bậc nhẹ trước:** `gemini-3.8-flash-medium` / `gemini-4-hydrogen-low` (report & docs ngắn); `gemini-4-hydrogen-medium` chỉ cho docs kiến trúc dài | Cập nhật docs + tổng hợp `.agents/reports/<ID>.md` sau Reviewer, trước `copilot-git` | Sửa code nghiệp vụ |
| 3 | **Thợ cơ khí (Workers)** | `opencode-go/deepseek-v4.1-flash` (`worker-fast/mid`), `opencode-go/deepseek-v4-pro` (`worker-strong` sau qualification); miễn phí/local đã duyệt làm dự phòng | Sửa đúng `files_allowed` trong worktree; **Write task logs, implementation findings** vào `.agents/log/<ID>.md` | Sửa ngoài phạm vi, lách ESLint/CI, sửa `BRANCHES.md`, `published/`, `.ua/`, hoặc tự sinh agent |
| 4 | **Audit & Explore** | Audit: **script/CI** trước; Explore tương tác: `opencode-go/deepseek-v4.1-flash` | Chạy lint (`--max-warnings 0`), test, diff check; tìm kiếm chỉ đọc | Sửa mã sản phẩm |

*Ghi chú:* Nhánh thử nghiệm **NVIDIA nền chỉ đọc** (không chặn merge, tối đa 1 việc thủ công) xem riêng tại [docs/NVIDIA-BACKGROUND.md](docs/NVIDIA-BACKGROUND.md).

### 1.2. Chính sách Ponytail theo Agent

Áp dụng [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) theo vai trò — **một nguồn sự thật duy nhất**:

| Agent | Mức Ponytail | Lý do |
|---|---|---|
| **Claude CEO** | `lite` hoặc `off` | Không để nguyên tắc tối giản hạn chế việc thiết kế giải pháp và đánh giá nhiều phương án |
| **Codex Reviewer** | `full` | Tập trung correctness, security, testing, performance và complexity |
| **Codex Developer** | `full` | Xử lý các thay đổi nhỏ, tránh sửa lan sang phần khác |
| **Antigravity** | Thiết kế `off` / `lite`; triển khai và review `full` | Chọn mức theo vai đang thực hiện, không áp `full` cho mọi giai đoạn |
| **Free Workers** | Quy tắc rút gọn | Giữ prompt ngắn, giới hạn phạm vi sửa đổi |
| **Toàn hệ thống** | **Không dùng `ultra` mặc định** | Tránh việc agent chất vấn hoặc thu hẹp yêu cầu đã được CEO phê duyệt |

**Quy tắc rút gọn cho Free Workers (4 dòng — nhúng trực tiếp vào prompt worker):**
1. **Khóa phạm vi:** Chỉ làm đúng Task Brief và chỉ sửa `files_allowed`; không mở rộng hay thu hẹp yêu cầu CEO đã duyệt.
2. **Đọc & tái sử dụng trước:** Đọc tệp mục tiêu trước khi sửa; tận dụng helper/component sẵn có thay vì thêm abstraction hoặc dependency mới.
3. **Không cắt an toàn:** Giữ nguyên validation ở ranh giới tin cậy, xử lý lỗi, bảo mật và thêm ít nhất 1 test cho logic có nhánh/vòng lặp/parser.
4. **Báo cáo trung thực:** Trả kết quả đúng mẫu ngắn gọn, ghi rõ phần nào chưa kiểm hoặc bị bỏ qua.

*Xử lý `shortcut:` của Ponytail:* Cuối sprint/trước release, Claude CEO chạy `/ponytail-debt` để chốt các nợ kỹ thuật có giá trị vào `.agents/memory/published/gotchas.md`.

### 1.3. Skill & Bậc model cho thợ

- **Chọn theo giai đoạn và vai:** dùng [ma trận Skill Routing](docs/SKILL-ROUTING.md) cho Claude, Codex, agy và workers. Ponytail là lớp hành vi bổ trợ, không thay thế workflow thiết kế, coding, debug, review hay verification.
- **Mặc định gọn:** một skill quy trình + một skill chuyên môn phù hợp; đây **không phải trần cứng 2 skill**. Thêm skill cần thiết theo giai đoạn và ghi lý do trong Brief/log; task cơ khí có thể không cần skill quy trình. Không bỏ `verification-before-completion` hay yêu cầu bắt buộc của client để đủ số lượng.
- **Nạp khi cần:** giữ danh mục skill khả dụng, đọc nội dung khi đúng giai đoạn; đổi giai đoạn không có nghĩa xóa skill hoặc gỡ nội dung đã đọc khỏi context. Worker giữ đúng vai và phạm vi đã duyệt, không tự sinh agent.
- **Phân biệt review:** quản lý dùng `requesting-code-review`, reviewer độc lập dùng `code-review`, implementer sửa theo feedback dùng `receiving-code-review`. Ponytail review chỉ bổ trợ về độ phức tạp, không tạo thêm cổng bắt buộc.
- **Kiểm khả dụng trước giao việc:** ghi tên skill và đường dẫn/ID thực của runtime trong `skills:` / `skill_refs:`. Skill có trên Codex không đồng nghĩa đã có trên Claude/OpenCode/agy; thiếu thì báo và dùng playbook tương đương đã duyệt hoặc chặn giai đoạn, không tự cài.
- **Bậc thợ (`tier:`):**
  - `nhanh` (≤ 2–3 tệp rõ đặc tả, có lint/test) và `vừa` (3–5 tệp) → ưu tiên `opencode-go/deepseek-v4.1-flash`.
  - `mạnh` (đa tệp phức tạp, hoặc sau 2 lần fail ở Flash) → `opencode-go/deepseek-v4-pro` (sau qualification) hoặc Codex (`openai/gpt-6.1-sol`) cho ca engineering khó.
- Khai báo agent OpenCode v2 lưu tại **`.opencode/agent/<tên>.md`** (thống nhất số ít theo repo này; truyền `--model` tường minh khi dùng `opencode run`; khóa quyền `edit`/`shell` không chạm `.agents/memory/published/` và `.ua/`).
- Kiểm soát hàng rào tĩnh bắt buộc: [docs/ESLINT-CI-GUARDRAILS.md](docs/ESLINT-CI-GUARDRAILS.md).

---

## 2. Lớp ngữ cảnh Understand-Anything (`.ua/`) & Bộ nhớ dùng chung

### 2.1. Understand-Anything (`.ua/`) — Thay thế hoàn toàn GitNexus

Sử dụng [Egonex-AI/Understand-Anything](https://github.com/Egonex-AI/Understand-Anything) để dựng Knowledge Graph dùng chung một lần cho toàn đội:

1. **Cài đặt trên các repository chính:**
   - **Claude Code:** `/plugin marketplace add Egonex-AI/Understand-Anything` → `/plugin install understand-anything`
   - **Windows (PowerShell — chọn `codex` / `antigravity` / `opencode`):** `iwr -useb https://raw.githubusercontent.com/Egonex-AI/Understand-Anything/main/install.ps1 | iex`
   - **Linux/macOS:** `curl -fsSL https://raw.githubusercontent.com/Egonex-AI/Understand-Anything/main/install.sh | bash -s <codex|antigravity|opencode>` *(lưu ý: Codex gọi bằng tiền tố `$`, ví dụ `$understand-chat`, `$understand-diff`)*
2. **Phân tích toàn bộ một lần:** chạy `/understand` (khuyên dùng `--language en` để tối ưu token và thống nhất thuật ngữ) để tạo `.ua/knowledge-graph.json`.
3. **Chia sẻ & `.gitignore`:** commit `.ua/` vào repo (nếu >10 MB bật `git lfs track ".ua/*.json"`), loại trừ tệp nháp cục bộ:
   ```gitignore
   .ua/intermediate/
   .ua/diff-overlay.json
   ```
4. **Quy tắc dùng giữa các agent:**
   - **Claude, Codex, Agy:** truy vấn `.ua/` qua skill (`/understand-chat`, `/understand-diff`, `$understand-chat`) trước khi đọc sâu mã.
   - **Workers:** **không đọc trực tiếp toàn bộ `.ua/knowledge-graph.json`** (tránh tràn ngữ cảnh). Nhận danh sách 3–5 tệp mục tiêu trong Task Brief hoặc truy vấn đọc qua MCP server của Understand-Anything.
   - **Cập nhật đồ thị:** **không** bật `--auto-update` trên từng commit worktree (tránh conflict JSON). Chỉ **Claude CEO** chạy incremental `/understand` **theo batch** trên `main` sau khi merge thay đổi cấu trúc/module.
5. **Tách ranh giới tri thức:** `.ua/` lưu **WHAT & WHERE** (cấu trúc mã, dependency, diff impact); `.agents/memory/published/` lưu **WHY & MUST-NOT** (quyết định, bất biến, bẫy ẩn). Không chép lại cây thư mục vào bộ nhớ dài hạn.

### 2.2. Bộ nhớ dùng chung (Shared Memory) & Ma trận Phân quyền

Lưu giữ các thông tin có giá trị qua nhiều task trong `.agents/memory/`:

```text
.agents/memory/
├── published/              # [CHỈ CLAUDE CEO ĐƯỢC GHI] Tri thức chuẩn qua nhiều task
│   ├── decisions.md        # Quyết định kiến trúc (ADRs: vì sao chọn/bỏ)
│   ├── invariants.md       # Bất biến, ranh giới bảo mật, quy ước bắt buộc
│   └── gotchas.md          # Bẫy kỹ thuật, lỗi lặp lại, nợ kỹ thuật đã chốt
├── HANDOFF.md              # [CLAUDE CEO] ≤ 30 dòng trạng thái dở dang cuối phiên
└── inbox/                  # [CODEX & AGY GHI — XÓA NGAY SAU KHI CEO XỬ LÝ]
    └── <ID>-<agent>.md     # Ghi rõ type: proposal | conflict
```

| Agent | Quyền | Đường dẫn được ghi |
|---|---|---|
| **Claude CEO** | **Approve, publish, update** | `.agents/memory/published/**`, `.agents/memory/HANDOFF.md`, `.ua/**`, dọn `inbox/` |
| **Codex** | **Propose, write task findings** | `.agents/memory/inbox/<ID>-codex.md`, `.agents/log/<ID>.md`, `.agents/reviews/<ID>-*.md` |
| **Agy** | **Propose, review, flag conflicts** | `.agents/memory/inbox/<ID>-agy.md`, `.agents/reviews/<ID>-*.md`, `.agents/log/<ID>.md` |
| **Workers** | **Write task logs, implementation findings** | `.agents/log/<ID>.md` |

*Quy tắc 90/10:* Task thông thường không cần tạo tệp `inbox/`. Chỉ tạo khi phát hiện tri thức tái sử dụng qua nhiều task hoặc mâu thuẫn với `published/`. Sau khi CEO phê duyệt/gộp hoặc bác bỏ, xóa ngay tệp trong `inbox/`.

### 2.3. Chuẩn hóa Báo cáo & Hạn chế truyền Lịch sử Hội thoại

**Nguyên tắc:** **Không forward lịch sử hội thoại giữa các agent.** Mọi bàn giao đều đi qua tệp `.agents/` theo mẫu ngắn (≤ 300 từ) + tin nhắn tín hiệu `agm send` (1–3 dòng trỏ đường dẫn tệp).

- **Task Brief (`.agents/tasks/<ID>.md`):** ghi `task_id`, `owner`, `tier`, `phase`, `work_type`, `ponytail`, `skills` (một dòng phân tách bằng dấu phẩy; mặc định một workflow + một domain), `skill_refs` (ID/đường dẫn thực đã kiểm), `skill_reason` khi bổ sung, `files_allowed`, `refs` (mục trong `.ua/` và `published/`), mục tiêu, ngoài phạm vi, `verify` / `Done when`, và `Previous blockers` khi sửa lại. Xem [mẫu feature, bugfix/review-fix và task cơ khí](docs/SKILL-ROUTING.md#5-task-brief-and-phase-changes). Verification cuối giai đoạn luôn được giữ.
- **Task Log / Findings (`.agents/log/<ID>.md`, chỉ thêm):** tóm tắt thay đổi (≤ 5 ý), tệp đổi, lệnh kiểm + số đếm PASS/FAIL, phần chưa kiểm, và `Long-term finding` (nếu có).
- **Review / Conflict Report (`.agents/reviews/<ID>-<n>.md`):** `verdict` (`ĐẠT | CHƯA ĐẠT | CONFLICT`), danh sách Blocker kèm `file:line` + hậu quả + cách sửa tối thiểu, cờ xung đột bộ nhớ (nếu có), và trỏ `inbox/` (nếu đề xuất cập nhật memory).
- **Chặn vòng lặp vô hạn:** Tối đa **2 vòng `CHƯA ĐẠT`** → vẫn lỗi thì **leo thang ngay về Claude CEO**.

---

## 3. Điều phối, Kinh phí & Giao tiếp (`agm` + `hc`)

### 3.1. Trách nhiệm & Chống giẫm chân

- **Định tuyến thực thi:** Workers làm việc cơ học 1–2 tệp; Agy triển khai tính năng chính/đa tệp theo kế hoạch; Codex Dev xử lý hotfix/surgical patch/ca khó sau 2 lần fail.
- **Review chéo (No Self-Review):** Không agent nào tự duyệt code của mình. Nhánh thường chỉ duyệt **1 tầng độc lập** (mặc định Pro Go; khác họ nếu Pro đã viết code). Chỉ chạy 2 tầng review cho ca rủi ro cao (auth, payment, schema/DB migration).
- **Mỗi tệp/vùng mã chỉ một nhánh sửa tại một thời điểm** (theo dõi qua `BRANCHES.md`).

#### 3.1.1. Điều phối kinh phí AI

1. **Người dùng đặt trần và duyệt ngoại lệ. Claude CEO chịu trách nhiệm phân bổ** cho từng việc/lane, giữ phần dự phòng cho review/sửa lại, và được hạ bậc/xếp lại hàng đợi/đổi nguồn trong phạm vi đã duyệt. Không tự bật **Extra Usage**, tăng trần, nâng gói hoặc dùng nguồn phát sinh phí ngoài gói.
2. **Không đếm trùng hạn mức dùng chung:** nhiều lane chia sẻ cùng gói Go chỉ định **1 CEO đầu mối** theo dõi tổng phần đã dùng + đang cấp + dự phòng.
3. **Kiểm hạn mức trước mỗi đợt giao/review/nâng bậc:** chạy **`agm quota --refresh`** (đọc Claude, Codex, `agy`, và OpenCode Go `5h`/`7d`/`mo`) và xác nhận trạng thái Extra Usage trên [OpenCode Go Console](https://opencode.ai/console/go). `agm cost --by source` và `agm budget` theo dõi token, không thay hóa đơn. Nếu dữ liệu stale hoặc thiếu → không coi là còn hạn mức, nhờ người dùng xác nhận.
4. **Ngưỡng cảnh báo (gợi ý 70% đã dùng):** giữ phần còn lại cho việc ưu tiên và review/rework. Cạn hạn mức thì chuyển nguồn miễn phí/cục bộ đủ năng lực đã duyệt hoặc đặt `blocked`; **không bỏ review** để tiết kiệm chi phí. Cần tăng chi thì hỏi người dùng qua `agm send user --type HỎI --task <ID> "..."`.
5. **Đối chiếu sau đợt chạy:** ghi nguồn/model/phần đã cấp và số liệu thực đo vào báo cáo cuối `.agents/reports/<ID>.md`; không đưa khóa API hay ảnh chụp thanh toán vào repo.

### 3.2. Giao tiếp qua `agm` và Kiểm git tại mọi bàn giao

- **Kênh chuẩn trong `herdr-core`:** `agm send <đích> --type GIAO|BÁO CÁO|LỖI|HỎI|CHẶN|ĐẠT|CHƯA ĐẠT --task <ID> "<1–2 câu; xem tệp>"` (cần người dùng quyết → đích `user` vào `agm inbox` / `agm tui`). Tên terminal & `<ID>`: chữ thường `a-z 0-9 - _`, ≤ 32 ký tự.
- **Mã tin & MCP (`agm 0.4.0`):** `agm send` / `agm_send` nhận cả mã tiếng Anh `ASSIGN|REPORT|ERROR|QUESTION|BLOCKED|PASS|FAIL` (CSDL lưu mã Việt tương đương). Chỉ gắn MCP server `agm` (`agm mcp`: `agm_send`, `agm_inbox`, `agm_ps`, `agm_quota`, `agm_spawn`, `agm_branch_get`, `agm_branch_update`) cho profile quản lý/duyệt (CEO, `copilot-git`, `reviewer`); **không gắn cho `worker-*`** vì có `agm_spawn`.
- **Ghi `BRANCHES.md`:** chỉ qua `agm branch set` / `agm_branch_update` (agm tự khóa; cấm `mkdir` khóa tay; terminal vai `worker` bị từ chối). Chi tiết: [BOT-CEO.md](BOT-CEO.md#3-giao-thức-branchesmd-và-khóa-ghi-song-song).
- **`agm spawn` thoát mã 3:** terminal đã mở nhưng chưa nhận brief (brief nằm trong `agm inbox`, mục `brief_failed`). Không dọn worktree, không spawn lại; gửi brief cho đúng terminal đó.
- **Xoay vòng ngữ cảnh:** [CEO và Codex High chủ động yêu cầu rotate ở 50%](docs/CONTEXT-ROTATION.md), chỉ dùng khoảng 50–55% để đến điểm bàn giao an toàn; task dài/tốn ngữ cảnh chọn sớm, thường 40% (khoảng 35–45%) hoặc thấp hơn khi dự báo phần tiếp theo cần nhiều chỗ. Phiên tự gọi `agm rotate <terminal-của-mình>` sau khi người dùng đã bật `agm manage`; ghi handoff theo daemon rồi mở phiên kế nhiệm. `agm 0.4.0` chưa có ngưỡng daemon riêng theo vai: `ctx_rotate = 70` chung vẫn là fallback cho workers thường, không phải mốc chờ của CEO/Codex High. **Không bao giờ tự kill Claude/Codex**; chỉ người dùng dừng bằng `agm kill` + xác nhận `y`.
- **Kiểm git trước mỗi cổng:** `git status` sạch, `git diff --stat` khớp danh sách tệp trong `tasks/<ID>.md` và `log/<ID>.md`, commit bắt đầu bằng `<ID>:`, và nhánh không phải CEO **không chạm** `.agents/memory/published/` hay `.ua/`.

---

## 4. Cổng phát hành (đúng thứ tự)

1. **Commit trên worktree** (code + test + `.agents/log/<ID>.md`).
2. **Audit (script/CI):** `eslint --max-warnings 0` / `ruff` / `clippy -D warnings`, test đạt 100%, chặn `eslint-disable`/`@ts-ignore`, kiểm diff đúng phạm vi & không chạm `published/`/`.ua/`.
3. **Reviewer (1 tầng độc lập):** đọc task + diff + log + `.ua/`, ghi `.agents/reviews/<ID>-<n>.md` (tối đa 2 vòng `CHƯA ĐẠT` → escalate CEO).
4. **Docs & Báo cáo cuối (nếu có thay đổi):** `agy` (`gemini-3.8-flash-medium` / `hydrogen-low`; `hydrogen-medium` chỉ cho docs kiến trúc dài) cập nhật docs + `.agents/reports/<ID>.md` và commit lấy `head` SHA cuối.
5. **Git Integration (`copilot-git`):** merge tuần tự đúng `head` SHA vào `main`, chạy lại Audit, dọn worktree/nhánh đã merge.
6. **Chốt tri thức & Đồ thị (Claude CEO):** duyệt `.agents/memory/inbox/` → publish vào `.agents/memory/published/` (xóa inbox), cập nhật `HANDOFF.md`, và chạy batch incremental `/understand` khi có thay đổi cấu trúc.
7. **Phát hành:** chỉ người dùng phê duyệt push/tag/deploy.

> **Guard `status: ready` (agm ≥ 0.4.1):** `agm branch set ... status=ready --worktree <wt>` kiểm `head` là SHA hợp lệ, worktree sạch (bỏ qua `.agents/tasks/`, `.agents/.runtime/`), HEAD khớp `head` và có bằng chứng review `review=.agents/reviews/<ID>-<n>.md@<sha>` (token đầu; phía sau được ghi chú tự do): tệp review phải đã commit ở HEAD, nhắc 7 ký tự đầu của `<sha>`, và từ `<sha>` tới HEAD không đổi tệp nào ngoài `.agents/`. Guard chứng minh Reviewer **đã chạy trên đúng mã đó**, nhưng **không** đọc verdict và không kiểm Audit (`verify=` vẫn là văn bản tự do). CEO chỉ đặt `ready` khi review ĐẠT và Audit xanh.

---

## 5. Cách nhúng gọn vào dự án (`AGENTS.md` / `CLAUDE.md`)

**Không dán nguyên toàn bộ tài liệu này vào `AGENTS.md` / `CLAUDE.md` của dự án** (tránh lãng phí token ở mỗi lượt worker). Chỉ đặt một **bản tóm tắt ≤ 80 dòng** tại gốc repo gồm:
1. Đường dẫn trỏ tới `AGENTS-TUTORIAL.md` và `.agents/memory/published/` (`decisions.md`, `invariants.md`, `gotchas.md`).
2. Lệnh `agm send` / `agm inbox` / `agm branch get|set` (hoặc tool MCP tương ứng cho vai quản lý) và quy tắc không tự `agm kill` Claude/Codex.
3. Ranh giới quyền ghi (cấm non-CEO sửa `.agents/memory/published/` và `.ua/`) + mức Ponytail theo vai.
4. Lệnh lint/test bắt buộc của dự án; trỏ [Skill Routing](docs/SKILL-ROUTING.md), mặc định một workflow + một domain và giữ verification khi kết thúc.

## Nguồn tham khảo

- `agmag` (`agm` v0.4.0): https://github.com/wosonday/agmag/ · [docs/AGM-0.4.0-GUIDE.md](docs/AGM-0.4.0-GUIDE.md) · [HERDR-CORE-BOT.md](HERDR-CORE-BOT.md) · [BOT-CEO.md](BOT-CEO.md) · [docs/README.md](docs/README.md)
- Understand-Anything: https://github.com/Egonex-AI/Understand-Anything
- Ponytail: https://github.com/DietrichGebert/ponytail
- herdr-core (`hc`): https://github.com/herdrdev/herdr-core
- OpenCode Go & docs: https://opencode.ai/docs/go/
