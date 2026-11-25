# Đặc tả nâng cấp `agm` (`agmag` v0.2.0 → v0.3.0): MCP Server + Durable Workflow Engine

**Ngày:** 2026-11-22 · **Trạng thái:** Phase 1 (`agm mcp` + `agm branch`) đã triển khai thành `agm 0.4.0` ([docs/AGM-0.4.0-GUIDE.md](docs/AGM-0.4.0-GUIDE.md)); Phase 2–3 đang mở

**Nền tảng nền:** `agmag` v0.2.0 → v0.4.0 (Rust 2024, `rusqlite` SQLite WAL, không async runtime, Windows 11 + Linux/WSL)

**Tài liệu liên quan:** [AGENTS-TUTORIAL.md](AGENTS-TUTORIAL.md) · [BOT-CEO.md](BOT-CEO.md) · [HERDR-CORE-BOT.md](HERDR-CORE-BOT.md) · [docs/ESLINT-CI-GUARDRAILS.md](docs/ESLINT-CI-GUARDRAILS.md) · [docs/AGM-0.4.0-GUIDE.md](docs/AGM-0.4.0-GUIDE.md)

---

## 1. Bối cảnh & Động lực nâng cấp

Hiện tại `agm` v0.2.0 đã giải quyết tốt phần **quan sát token/hạn mức, hộp thư người dùng, nhắn tin `agm send`, sinh thợ `agm spawn` và xoay vòng phiên (`rotation::tick`)**. Tuy nhiên khi vận hành bộ tài liệu mới theo mô hình **1 + 2 + 4 + 5** (gói thuê bao trong `herdr-core` + hàng rào lint tĩnh + workflow bền vững + MCP/Skills), có 4 điểm nghẽn thực tế:

1. **Agent gọi CLI dễ lỗi cú pháp trên Windows PowerShell:** phải truyền cờ `--type`, dấu ngoặc kép lồng nhau khi dùng `agm send` / `agm spawn`, và BOT-CEO phải tự chạy bash `mkdir .agents/.runtime/locks/branches` để khóa `BRANCHES.md` (dễ kẹt lock nếu phiên crash giữa chừng).
2. **Vòng lặp `inbox-watch.sh` (`while true; sleep 10`) không bền vững:** rớt khi reboot, không theo dõi được task đang ở bước nào (`Worker` → `Gate` → `Reviewer` → `Docs` → `Ready`), và chưa tự động chạy **Deterministic Gate (0 token LLM)** trước khi tốn token gọi Reviewer.
3. **Thiếu luồng fan-out / tự leo bậc `tier:` ở cấp daemon:** chưa tự động lấy `tier:` / `verify:` trong `.agents/tasks/<ID>.md`, chưa tự động bắt lỗi gate < 2 lần rồi nâng bậc `nhanh → vừa → mạnh` khi thất bại.
4. **Ngân sách theo provider mới chỉ cảnh báo:** như `README_vi.md` §12 đã ghi, `provider:codex:10080` chưa chặn hoặc điều phối được hành động khi vượt ngưỡng cứng.

---

## 2. Mục tiêu & Ràng buộc kiến trúc (v0.3.0)

### Mục tiêu
1. **Thêm `agm mcp` (Hướng 5 — MCP Server qua `stdio`):** chuẩn hóa toàn bộ thao tác điều phối của agent (`send`, `inbox`, `spawn`, `ps`, `quota`, `workflow`, `branch`) thành MCP tool có JSON Schema chặt chẽ, chạy đồng bộ trên `std::io` + `serde_json` (**không cần thêm `tokio`** vào `Cargo.toml`).
2. **Thêm `agm workflow` + `workflow::tick` trong `agm daemon` (Hướng 4 + Hướng 2):** biến `agm.db` (schema v4) thành **Durable State Machine** cho từng đầu việc song song: tự chạy worktree, gọi **Deterministic Gate (ESLint/pytest/cargo test — 0 token LLM)** ngay trong `agm`, tự vòng lại thợ nếu fail và tự nâng bậc `tier:` sau 2 lần fail, tạm dừng `Human Interrupt` khi gặp `blocked`, và chuyển trạng thái `ready`.
3. **Thêm `agm branch` (Khóa `BRANCHES.md` nguyên tử):** cấp lệnh CLI + MCP tool đọc/ghi khối `### <lane>/<slug>` trong `BRANCHES.md` bằng file lock / transaction an toàn trên cả Windows 11 và Linux.
4. **Tùy chọn chặn cứng cho `provider` budget (`--action block`):** hỗ trợ từ chối `agm spawn` với template dùng provider đã vượt ngưỡng cứng (khi cấu hình ánh xạ template/provider).

### Phản mục tiêu (Giữ nguyên kỷ luật cứng của v0.2.0)
- **Không phá luật bảo vệ Claude/Codex:** mọi lời gọi từ `agm mcp` đều đi qua `actions::Caller::Agent(...)` (fail-closed) — **tuyệt đối không** cho phép `agm mcp` gọi `kill`/`interrupt` lên `claude-code` hay `codex`, và không lộ `agm manage` / `agm cleanup` qua MCP.
- **Không bỏ gói thuê bao / `hc`:** `agm` vẫn điều khiển các CLI trong `herdr-core` (`hc`), không ép chuyển sang trả phí API key.
- **Không thêm phụ thuộc nặng:** giữ triết lý binary Rust đơn gọn, chạy `cargo test --offline` xanh toàn bộ.
- **Tương thích ngược 100%:** toàn bộ lệnh v0.2.0 (`daemon`, `tui`, `spawn`, `send`, `inbox`, `reply`, `done`, `manage`, `rotate`, `cleanup`, `budget`, `policy`, `ask`, `hook`, `scan`, `ls`, `cost`, `quota`, `log`, `ps`, `link`) giữ nguyên hành vi và cờ cũ.

---

## 3. Thiết kế chi tiết từng phần

### 3.1. Phần A — `agm mcp` (MCP Server qua `stdio`)

Thêm module `src/mcp.rs` và subcommand `Cmd::Mcp` trong `src/main.rs`.

- **Giao thức:** JSON-RPC 2.0 qua `stdin`/`stdout` theo chuẩn MCP (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`). Mọi log debug chỉ ghi ra `stderr`.
- **Nhận diện người gọi:** đọc `HC_TERMINAL_ID` từ env của tiến trình cha (hoặc cho phép truyền `caller` đọc-only để ghi log, nhưng **mọi thao tác nhạy cảm luôn ép `Caller::Agent`**).

| Tên MCP Tool | Bọc module hiện có | Đầu vào chính (JSON Schema) | Ghi chú an toàn |
|---|---|---|---|
| `agm_send` | `actions::send` | `to`, `kind` (`GIAO\|BÁO CÁO\|LỖI\|HỎI\|CHẶN\|ĐẠT\|CHƯA ĐẠT`), `task?`, `text` | Tự lấy `from` từ `HC_TERMINAL_ID`; đích `user` đẩy vào `inbox` |
| `agm_inbox` | `store.inbox` | `all?: bool` | Chỉ đọc hộp thư ở dạng JSON |
| `agm_ps` | `store.terminals` | `all?: bool` | Xem danh sách terminal + task + trạng thái |
| `agm_quota` | `report::quota` + `policy::task_tokens` | `task?: string` | Trả hạn mức 5h/7d của Claude/Codex và token của task (để BOT-CEO tự cân tải trước khi spawn) |
| `agm_spawn` | `actions::spawn` | `template`, `task`, `role?`, `name?`, `cwd?`, `text?` | Kiểm cứng `max_workers` (chỉ đếm `role=worker`) và ngân sách `task:<ID>` |
| `agm_workflow_submit` | `workflow::submit` (mới) | `task_id`, `repo_cwd`, `task_file?`, `gate_cmd?`, `tier?` | Nạp đầu việc vào hàng đợi bền vững của `agm daemon` |
| `agm_workflow_status` | `workflow::list` (mới) | `task_id?` | Xem trạng thái hiện tại, số lần fail gate, SHA |
| `agm_branch_update` | `branches::update` (mới) | `branches_file`, `slug`, `lane`, `status`, `head?`, `base?`, `depends_on?`, `touches_hotspot?`, `verify?`, `review?`, `docs?`, `summary?` | Ghi nguyên tử khối `### <slug>` trong `BRANCHES.md`; khóa không để worker sửa `BRANCHES.md` (chỉ cấp cho BOT-CEO / Copilot) |

**Cách khai báo phía client:**
- Claude Code: `claude mcp add -s user agm -- agm mcp`
- OpenCode (`opencode.json`): khai báo MCP `agm` kiểu `local`, lệnh `["agm", "mcp"]` (chỉ bật trong profile quản lý/review nếu không muốn worker thường gọi `agm_spawn` hoặc `agm_branch_update`).

---

### 3.2. Phần B — `agm workflow` & Deterministic Gate (State Machine bền vững, Schema v4)

Thêm `src/store/schema_v4.sql` (`SCHEMA_VERSION = 4` trong `src/store/mod.rs`) và module `src/workflow.rs`.

#### Bảng `task_runs` (Schema v4)
```sql
CREATE TABLE IF NOT EXISTS task_runs (
    task_id       TEXT PRIMARY KEY NOT NULL,   -- a-z 0-9 - _ (<= 27 ký tự)
    repo_cwd      TEXT NOT NULL,               -- đường dẫn checkout chính
    worktree_path TEXT NOT NULL,               -- vd .wt/<task_id> hoặc ../repo.wt/<slug>
    branch        TEXT NOT NULL,               -- vd feat/<task_id>
    tier          TEXT NOT NULL DEFAULT 'nhanh', -- nhanh | vừa | mạnh
    attempt       INTEGER NOT NULL DEFAULT 0,  -- số lần đã chạy qua gate
    state         TEXT NOT NULL,               -- trạng thái state machine (xem bảng dưới)
    terminal      TEXT,                        -- address/name terminal hiện tại (w-<id>-<n>)
    manager       TEXT,                        -- terminal quản lý để báo tin (vd ceo-tap-hoa)
    gate_cmd      TEXT,                        -- lệnh kiểm tĩnh (eslint + test); 0 token LLM
    review_tpl    TEXT,                        -- template reviewer (vd reviewer)
    docs_tpl      TEXT,                        -- template docs (vd docs-hydrogen)
    head_sha      TEXT,                        -- SHA đã qua cổng
    last_error    TEXT,                        -- tóm tắt lỗi gần nhất (<= 2000 ký tự)
    created_ms    INTEGER NOT NULL,
    updated_ms    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_runs_state ON task_runs(state);
```

#### Sơ đồ chuyển trạng thái trong `workflow::tick` (gọi mỗi chu kỳ của `daemon_loop` sau `rotation::tick`)

```text
  queued ──(tạo worktree + agm spawn worker theo tier)──► working
    ▲                                                        │
    │                                         (worker exit 0 / BÁO CÁO)
    │                                                        ▼
    │  (fail < 2: gửi lỗi gate về worker;            gate_checking (0 token LLM:
    │   fail == 2: nâng tier & spawn mới)             chạy gate_cmd + check-no-lint-bypass)
    └───────────────────────◄────────────────────────────────┤
                                                      (gate exit 0)
                                                             ▼
                                                        reviewing (agm spawn reviewer)
                                                             │
                                                   (ĐẠT; CHƯA ĐẠT -> về working)
                                                             ▼
                                                        docs_updating (nếu có docs_tpl)
                                                             │
                                                             ▼
                                                           ready (chốt head_sha, báo copilot-git / inbox)
```

**Quy tắc quan trọng của `workflow::tick`:**
1. **Tôn trọng `agm policy off`:** nếu `policy::enabled == false`, `workflow::tick` dừng ngay, không spawn hay chuyển trạng thái tự động.
2. **Song song đa luồng (Fan-out):** trong một chu kỳ, tất cả các bản ghi ở `queued` có thể spawn đồng thời cho đến khi `worker` đạt `[policy].max_workers` (chỉ đếm `role=worker`, không đếm `gate_checking`/`reviewing`/`docs_updating`).
3. **Deterministic Gate chạy thuần ngoài LLM (0 token):**
   - `agm` tự chạy `gate_cmd` (mặc định đọc từ trường `verify:` trong `.agents/tasks/<ID>.md` hoặc cấu hình `[workflow].default_gate` trong `providers.toml`) ngay trong `worktree_path` + kiểm tra `git diff` không chứa `eslint-disable`/`@ts-ignore`/`@ts-nocheck` hay thay đổi tệp `eslint.config.*`.
   - Nếu pass → spawn tiếp `review_tpl` (không tốn lượt BOT-CEO trung gian!).
   - Nếu fail → lưu log ngắn vào `last_error`: lần 1–2 gửi nguyên văn lỗi về `terminal` (hoặc mở lại worker); đến lần thứ 2 vẫn fail cùng `tier` → tự nâng `tier` (`nhanh → vừa → mạnh`), ghi vào `actions` và spawn worker bậc cao hơn (hoặc mở mục `inbox` nếu đã ở `mạnh` mà vẫn lỗi).
4. **Human Interrupt & Phục hồi sau reboot:**
   - Khi worker bị `blocked` (hỏi người dùng) hoặc task ở trạng thái `needs_attention`, workflow tạm dừng không tiêu thêm token cho đến khi bạn `agm reply` / `agm workflow resume <ID>`.
   - Khi máy khởi động lại và `agm daemon` bật lên, `workflow::tick` tiếp tục xử lý đúng các hàng `task_runs` đang dở trong `agm.db` (không tạo worktree/task trùng).

---

### 3.3. Phần C — `agm branch` (Quản lý `BRANCHES.md` có khóa)

Thêm module `src/branches.rs` và subcommand `Cmd::Branch`:
- Đọc/ghi khối `### <slug>` theo đúng định dạng `- khóa: giá trị` của `BRANCHES.md`.
- **Khóa nguyên tử đa tiến trình:** tạo file lock tạm cạnh tệp `BRANCHES.md` (hoặc trong thư mục `.agents/.runtime/locks/branches.lock` dùng `OpenOptions::create_new(true)` kết hợp kiểm tra PID/mùa quá hạn 60s để tự thu hồi nếu tiến trình chết, khắc phục nhược điểm chờ 10 phút của `mkdir`).
- **Quy tắc bảo vệ:** từ chối chuyển sang `status: ready` nếu không có `head` SHA 12+ ký tự, hoặc nếu worktree của nhánh đó còn thay đổi chưa commit (`git status --porcelain` khác rỗng).

---

### 3.4. Phần D — Nâng cấp cấu hình & TUI

1. **`~/.agmag/providers.toml` (tùy chọn bổ sung):**
   ```toml
   [workflow]
   # Ánh xạ tier -> template cho agm workflow
   fast_template = "worker-fast"
   mid_template = "worker-mid"
   strong_template = "worker-strong"
   reviewer_template = "reviewer"
   docs_template = "docs-hydrogen"
   # Lệnh gate mặc định nếu tasks/<ID>.md không ghi verify:
   default_gate = "npx eslint . --max-warnings 0 --report-unused-disable-directives"
   # Chặn thêm dòng bypass lint trong diff
   block_lint_bypass = true
   ```
   *(Lưu ý `Config` hiện đang dùng `#[serde(deny_unknown_fields)]`, nên cần thêm struct `WorkflowCfg` với `#[serde(default)]` vào `src/config.rs` để không vỡ tệp `providers.toml` cũ.)*
2. **Nâng cấp `agm budget` cho provider:** bổ sung `--action block` cho `provider:<tên>:<phút>` (tùy chọn), nếu vượt cứng thì `spawn_precheck` cảnh báo hoặc từ chối spawn template gắn với provider đó.
3. **Hiển thị trong `agm tui`:** thêm một mục ngắn `── WORKFLOWS ──` (hiện `task_id`, `tier`, `attempt`, `state`, `terminal`) để người dùng nhìn thấy cả đường ống chạy tự động ngay trên một màn hình.

---

## 4. Lộ trình triển khai đề xuất (chia 3 đợt, mỗi đợt tự kiểm chứng được)

| Đợt | Phạm vi | Tiêu chí nghiệm thu (`cargo test --offline` + test thật) |
|---|---|---|
| **Phase 1: `agm mcp` + `agm branch`** | `src/mcp.rs`, `src/branches.rs`, `Cmd::Mcp`, `Cmd::Branch` | Test JSON-RPC `initialize` / `tools/list` / `agm_send` / `agm_quota`; test khóa đồng thời nhiều thread ghi `BRANCHES.md` không mất dòng; `agm mcp` chặn mọi lệnh `kill` lên `claude-code`/`codex`; đăng ký chạy thử được trong Claude Code & OpenCode |
| **Phase 2: `schema v4` + `agm workflow` + Deterministic Gate** | `src/store/schema_v4.sql`, `src/workflow.rs`, `src/config.rs`, tích hợp vào `daemon_loop` | Test chuyển trạng thái đầy đủ bằng mock `Hc` & mock git/command; test fail gate 2 lần tự nâng `tier`; test `eslint-disable` trong diff bị gate chặn 0 token LLM; test reboot giả lập (`daemon` khởi động lại không spawn trùng) |
| **Phase 3: TUI Workflow + Provider Hard Block** | `src/dashboard.rs`, `src/tui.rs`, `src/policy.rs` | `agm tui` render sạch ở độ rộng 110 cột; test `agm budget set provider:... --action block`; cập nhật `README_vi.md` & `README.md` lên `0.3.0` |

---

## 5. Prompt mẫu để bạn gửi vào phiên triển khai `agmag`

```text
Hãy đọc AGENTS.md, docs/superpowers/specs/2026-11-18-agmag-design.md và tài liệu đề xuất nâng cấp AGM-UPGRADE-SPEC.md.
Mục tiêu: triển khai nâng cấp agm từ v0.2.0 lên v0.3.0 theo từng Phase (bắt đầu từ Phase 1: `agm mcp` stdio + `agm branch` khóa nguyên tử BRANCHES.md).
Ràng buộc cứng:
1. Giữ nguyên cấu trúc đồng bộ của Rust crate (không thêm tokio nếu chưa cần), chạy `cargo test --offline` và `cargo clippy --all-targets -- -D warnings` đạt 100%.
2. Giữ nguyên luật bảo vệ `actions::authorize`: mọi tool qua `agm mcp` luôn chạy với `Caller::Agent` (fail-closed), không bao giờ kill/interrupt Claude Code hay Codex.
3. Hãy lập kế hoạch chi tiết cho Phase 1 và hỏi tôi xác nhận trước khi sửa mã.
```
