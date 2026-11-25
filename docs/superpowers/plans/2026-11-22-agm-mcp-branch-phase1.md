# agm Phase 1 — `agm mcp` (stdio) + `agm branch` (khóa BRANCHES.md) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cho agent điều phối qua MCP tool có JSON Schema thay vì gõ CLI dễ lỗi trên PowerShell, và cho BOT-CEO/Copilot ghi `BRANCHES.md` dưới khóa nguyên tử tự thu hồi.

**Architecture:** Hai module mới trong lib: `src/branches.rs` (hàm thuần đọc/sửa khối `### <lane>/<slug>` + khóa thư mục + ghi tmp→rename) và `src/mcp.rs` (vòng JSON-RPC 2.0 đồng bộ, mỗi dòng một thông điệp, gọi thẳng `actions`/`store`/`report`/`branches`). `src/main.rs` chỉ thêm `Cmd::Branch` và `Cmd::Mcp` để nối dây. Không async, không phụ thuộc mới.

**Tech Stack:** Rust 2024, `serde_json`, `rusqlite` (qua `Store`), `std::io`, `std::fs`; test bằng `tests/common::FakeHc`, `tempfile`, `CARGO_BIN_EXE_agm`.

**Spec:** `D:\.harnessing\agent-tutorial\AGM-UPGRADE-SPEC.md` §3.1, §3.3 và dòng Phase 1 của §4 — đọc kèm mục "Điều chỉnh so với spec" dưới đây (spec viết trên v0.2.0, repo đang ở 0.3.3).

## Điều chỉnh so với spec (đã chốt trong kế hoạch này)

1. **Phiên bản đích là `0.4.0`**, không phải `0.3.0` (đã phát hành 0.3.0–0.3.3).
2. **Không đổi schema.** Phase 1 không cần bảng mới; `task_runs` để Phase 2 và sẽ là **schema v5** (v4 đã dùng cho reasoning effort).
3. **Loại tin MCP dùng enum tiếng Anh** `ASSIGN|REPORT|ERROR|QUESTION|BLOCKED|PASS|FAIL`; mã tiếng Việt vẫn nhận làm alias. Lưu bằng mã Việt như CLI (`actions::kind_from_english`).
4. **Tool workflow (`agm_workflow_submit`, `agm_workflow_status`) không có trong Phase 1** — chúng cần `workflow.rs` của Phase 2.
5. **Khóa dùng chính thư mục `.agents/.runtime/locks/branches`** (cùng chỗ với giao thức `mkdir` trong BOT-CEO.md §4.3) để agm và script `mkdir` cũ loại trừ lẫn nhau; khóa quá 60 giây coi là chết và được thu hồi.
6. **Khóa ghi `BRANCHES.md` được thực thi:** terminal gọi có `role = worker` trong `agm.db` bị từ chối (cả CLI lẫn MCP).
7. **`agm_branch_update` nhận `entry` dạng `<lane>/<slug>` + `fields` (map khóa→giá trị)** thay vì hai tham số `slug`/`lane` rời, vì ở BOT-CEO.md trường `lane:` (vd `ceo-tap-hoa`) khác tiền tố tiêu đề (vd `tap-hoa`).

## Global Constraints

- Không thêm crate nào vào `[dependencies]`; không `tokio`.
- `cargo test --offline`, `cargo clippy --all-targets -- -D warnings`, `cargo fmt --check` đều xanh sau mỗi task.
- `src/mcp.rs` không bao giờ dựng `Caller::User`; không có tool nào gọi `actions::kill`, `actions::interrupt`, `set_managed_launch`, `execute_cleanup_batch`, `reply` hay `dismiss`.
- Trong `agm mcp`, stdout chỉ chứa thông điệp JSON-RPC (mỗi dòng một JSON); mọi log ra stderr.
- Mọi lệnh v0.3.3 giữ nguyên hành vi và cờ.
- Thông báo lỗi người dùng thấy: tiếng Việt như phần còn lại của `actions`/`ops`; help của clap: tiếng Anh như các lệnh hiện có.
- Đường dẫn khóa: `<thư mục chứa BRANCHES.md>/.agents/.runtime/locks/branches`; thu hồi khi cũ hơn **60 s**; chờ tối đa **30 s**.
- Trạng thái hợp lệ: `wip blocked ready merging merged deployed needs-rebase failed`.
- Khóa trường hợp lệ: `status lane tier head base depends_on migration touches_hotspot summary verify review docs registered git`.

## Review Focus

1. **`BRANCHES.md` dùng CRLF trên Windows** → sửa một khối phải giữ CRLF cho toàn tệp, không trộn `\n`. (Test ở Task 1.)
2. **Khóa mồ côi do tiến trình chết hoặc do script `mkdir` cũ** → agm phải thu hồi khóa > 60 s và vẫn chờ khóa còn mới. (Test ở Task 2.)
3. **Không có `HC_TERMINAL_ID`** (MCP chạy ngoài herdr) → `agm_send` ghi `from = "agent"`, tuyệt đối không phải `"user"`. (Test ở Task 6.)
4. **Dòng JSON hỏng, method lạ, notification không có `id`** → trả đúng mã lỗi JSON-RPC (hoặc im lặng với notification) và vòng lặp không thoát. (Test ở Task 4.)
5. **Giá trị tiếng Việt và giá trị chứa `#`** (`tier: vừa`, `summary: sửa issue #12`) → đọc/ghi giữ nguyên byte; chỉ ` # ` (khoảng trắng–`#`–khoảng trắng) mới tách chú thích. (Test ở Task 1.)

---

### Task 1: `branches` — đọc và sửa khối trong văn bản (hàm thuần)

**Files:**
- Create: `src/branches.rs`
- Modify: `src/lib.rs` (thêm `pub mod branches;`)
- Test: `tests/branches_test.rs`

**Interfaces:**
- Produces:
  - `pub const STATUSES: [&str; 8]`, `pub const FIELDS: [&str; 14]` (giá trị ở Global Constraints)
  - `pub fn validate_entry(entry: &str) -> anyhow::Result<()>` — đúng một `/`; mỗi phần 1–64 ký tự `[a-z0-9._-]`
  - `pub fn validate_fields(fields: &[(String, String)]) -> anyhow::Result<()>` — khóa thuộc `FIELDS`, không trùng; giá trị sau `trim` khác rỗng, không chứa `\n`/`\r`; `status` thuộc `STATUSES`
  - `pub fn read_entry(text: &str, entry: &str) -> anyhow::Result<Option<Vec<(String, String)>>>`
  - `pub fn upsert_entry(text: &str, entry: &str, updates: &[(String, String)]) -> anyhow::Result<(String, bool)>` — trả `(văn bản mới, created)`

Quy tắc định dạng (implementer không tự chọn):
- Tiêu đề khối là dòng mà `trim_end()` bằng `### {entry}`. Khối kết thúc trước dòng kế tiếp bắt đầu bằng `#` (mọi cấp tiêu đề) hoặc EOF. Tiêu đề xuất hiện ≥ 2 lần → lỗi `"mục {entry} xuất hiện nhiều lần trong BRANCHES.md"`.
- Dòng trường: `- <khóa>: <giá trị>` với khóa `[a-z_]+`. Khi đọc, cắt chú thích tại lần xuất hiện đầu tiên của ` # ` hoặc `\t# ` rồi `trim`.
- Sửa khóa đã có: thay **cả dòng** bằng `- khóa: giá trị` (bỏ chú thích cũ). Khóa mới: chèn sau dòng trường cuối của khối (hoặc ngay sau tiêu đề), theo thứ tự trong `updates`.
- Mục mới: thêm vào cuối tệp, cách nội dung trước đúng một dòng trống, tệp luôn kết thúc bằng một xuống dòng.
- Kiểu xuống dòng: nếu `text` chứa `\r\n` thì toàn bộ đầu ra dùng `\r\n`, ngược lại `\n`.

- [ ] **Step 1: Viết test hỏng**

```rust
#[test] fn read_entry_strips_inline_comment_but_keeps_hash_in_value() {
    let t = "### tap-hoa/low-stock\n- head: 9f8e7d6c5b4a      # SHA cuối\n- summary: sửa issue #12\n- tier: vừa\n";
    let f = read_entry(t, "tap-hoa/low-stock").unwrap().unwrap();
    assert_eq!(f, vec![("head".into(),"9f8e7d6c5b4a".into()),("summary".into(),"sửa issue #12".into()),("tier".into(),"vừa".into())]);
}
#[test] fn upsert_replaces_existing_and_appends_new_keys_inside_block_only() {
    let t = "# Sổ\n\n### a/x\n- status: wip\n\n### b/y\n- status: wip\n";
    let (out, created) = upsert_entry(t, "a/x", &[("status".into(),"blocked".into()),("summary".into(),"chờ".into())]).unwrap();
    assert!(!created);
    assert_eq!(out, "# Sổ\n\n### a/x\n- status: blocked\n- summary: chờ\n\n### b/y\n- status: wip\n");
}
#[test] fn upsert_new_entry_appends_with_one_blank_line() {
    let (out, created) = upsert_entry("# Sổ\n", "a/x", &[("status".into(),"wip".into())]).unwrap();
    assert!(created);
    assert_eq!(out, "# Sổ\n\n### a/x\n- status: wip\n");
}
#[test] fn upsert_preserves_crlf() {
    let (out, _) = upsert_entry("### a/x\r\n- status: wip\r\n", "a/x", &[("status".into(),"ready".into())]).unwrap();
    assert_eq!(out, "### a/x\r\n- status: ready\r\n");
}
#[test] fn duplicate_heading_is_an_error() { /* hai khối "### a/x" → upsert_entry trả Err chứa "nhiều lần" */ }
#[test] fn validation_rejects_bad_input() {
    assert!(validate_entry("a/b/c").is_err()); assert!(validate_entry("A/x").is_err()); assert!(validate_entry("a/x").is_ok());
    assert!(validate_fields(&[("status".into(),"done".into())]).is_err());
    assert!(validate_fields(&[("owner".into(),"x".into())]).is_err());
    assert!(validate_fields(&[("summary".into(),"a\nb".into())]).is_err());
    assert!(validate_fields(&[("summary".into(),"  ".into())]).is_err());
}
```

- [ ] **Step 2: Chạy, xác nhận hỏng** — `cargo test --offline --test branches_test` → lỗi biên dịch "unresolved import `agmag::branches`".
- [ ] **Step 3: Cài đặt 5 hàm trên trong `src/branches.rs`** — tách văn bản bằng `split_inclusive('\n')` để giữ nguyên các dòng không chạm tới.
- [ ] **Step 4: Chạy lại** — cùng lệnh → PASS.
- [ ] **Step 5: Commit** — `git add src/branches.rs src/lib.rs tests/branches_test.rs && git commit -m "feat(branch): parse and upsert BRANCHES.md entries"`

---

### Task 2: `branches` — khóa thư mục, ghi nguyên tử, `update` có bảo vệ

**Files:**
- Modify: `src/branches.rs`
- Test: `tests/branches_test.rs`

**Interfaces:**
- Consumes: Task 1; `agmag::git::Git` (`head`, `is_clean`); `Store::terminal`, `Store::log_action`.
- Produces:
  - `pub fn lock_dir(file: &Path) -> PathBuf`
  - `pub struct BranchLock { /* path */ }` — `Drop` xóa thư mục khóa
  - `pub fn acquire_lock(dir: &Path, timeout: Duration, stale_after: Duration) -> anyhow::Result<BranchLock>`
  - `pub fn ensure_writer(store: &Store, caller_terminal: Option<&str>) -> anyhow::Result<()>`
  - `#[derive(Debug, Clone)] pub struct BranchUpdate { pub file: PathBuf, pub entry: String, pub fields: Vec<(String, String)>, pub worktree: Option<String> }`
  - `#[derive(Debug, Clone, serde::Serialize)] pub struct UpdateOutcome { pub entry: String, pub created: bool, pub fields: Vec<(String, String)> }` (`fields` = toàn bộ trường của mục sau khi ghi)
  - `pub fn update(store: &Store, git: &dyn Git, caller_terminal: Option<&str>, req: &BranchUpdate, timeout: Duration, now_ms: i64) -> anyhow::Result<UpdateOutcome>`
  - `pub fn get(file: &Path, entry: &str) -> anyhow::Result<Option<Vec<(String, String)>>>`
  - `pub const LOCK_TIMEOUT: Duration = 30s`, `pub const LOCK_STALE: Duration = 60s`

Quyết định đã chốt:
- `acquire_lock`: `create_dir_all(parent)` rồi `create_dir(dir)`; thành công thì ghi `dir/owner` = `"{pid} {now_ms}"` (chỉ để chẩn đoán). `AlreadyExists` → nếu `mtime` của thư mục cũ hơn `stale_after`: `rename(dir, dir.with_extension(format!("stale-{pid}-{nanos}")))` rồi `remove_dir_all` bản đã đổi tên (đổi tên chỉ một tiến trình thắng), thử lại ngay. Ngược lại ngủ 100 ms; quá `timeout` → lỗi `"BRANCHES.md đang bị khóa tại {dir} (quá {timeout}s)"`.
- `ensure_writer`: `caller_terminal` có và `store.terminal(t)?` có `role == Some("worker")` → lỗi `"thợ ({t}) không được ghi BRANCHES.md — báo BOT-CEO qua agm send"`. Không có terminal hoặc role khác → cho qua.
- `update`, theo thứ tự: `validate_entry` → `validate_fields` → tên tệp phải là `BRANCHES.md` → `ensure_writer` → lấy khóa → đọc tệp (thiếu thì coi là rỗng) → `upsert_entry` → nếu `updates` có `status = ready` thì kiểm **bảo vệ ready** trên trường sau gộp → ghi `BRANCHES.md.tmp-{pid}` rồi `rename` đè → nhả khóa → `log_action(who, "branch", entry, "k=v, …", ok, now)` cả khi thành công lẫn thất bại (`who` = terminal hoặc `"user"`).
- Bảo vệ ready: `head` khớp `^[0-9a-f]{12,40}$`; `req.worktree` bắt buộc; `git.is_clean(worktree)? == true`; `git.head(worktree)?` bắt đầu bằng `head`. Vi phạm nào cũng là lỗi và không ghi tệp.

- [ ] **Step 1: Viết test hỏng** (dùng `FakeGit { head, clean }` khai báo trong file test, `Store::open_in_memory`, `tempfile::tempdir`)

```rust
#[test] fn update_writes_entry_and_logs_action() { /* update(... fields status=wip ...) → tệp có "### a/x\n- status: wip"; created == true; thư mục khóa không còn */ }
#[test] fn ready_requires_head_worktree_clean_and_matching_sha() {
    // head="abc" → Err; thiếu worktree → Err; FakeGit{clean:false} → Err;
    // FakeGit{head:"deadbeef0000ffff", clean:true}, head="deadbeef0000" → Ok
    // mỗi lần Err: nội dung tệp không đổi
}
#[test] fn worker_terminal_is_refused() { /* upsert TerminalRow{address:"t9", role:Some("worker")} → update(..Some("t9")..) Err chứa "thợ"; tệp không được tạo */ }
#[test] fn stale_lock_is_reclaimed_fresh_lock_times_out() {
    // create_dir(lock_dir) thủ công: acquire_lock(dir, 300ms, 60s) → Err chứa "đang bị khóa"
    // acquire_lock(dir, 2s, Duration::ZERO) → Ok (coi khóa có sẵn là cũ)
}
#[test] fn concurrent_updates_lose_nothing() {
    // 8 thread, mỗi thread mở Store riêng trên cùng tệp sqlite tạm, update entry "l/s{i}";
    // sau join: read_entry đủ 8 mục, không còn tệp *.tmp-* trong thư mục
}
```

- [ ] **Step 2: Chạy, xác nhận hỏng** — `cargo test --offline --test branches_test` → lỗi biên dịch thiếu `update`/`acquire_lock`.
- [ ] **Step 3: Cài đặt các hàm ở Interfaces** theo "Quyết định đã chốt".
- [ ] **Step 4: Chạy lại** — PASS (chạy 3 lần liên tiếp để bắt test song song chập chờn).
- [ ] **Step 5: Commit** — `git commit -m "feat(branch): atomic locked update with ready guard and worker refusal"`

---

### Task 3: CLI `agm branch get|set`

**Files:**
- Modify: `src/main.rs` (thêm `Cmd::Branch { cmd: BranchCmd }`, nhánh match), `src/ops.rs` (hàm `branch_get`, `branch_set`)
- Test: `tests/cli_test.rs`

**Interfaces:**
- Consumes: `branches::{get, update, BranchUpdate, LOCK_TIMEOUT}`, `git::GitCli`, `ops::hc_terminal`.
- Produces (CLI):
  - `agm branch get <ENTRY> [--file PATH]` → in các trường (`--json`: object `{"entry", "fields": [[k,v],…]}`); không có mục → exit 1, `agm: error: không có mục <ENTRY> trong <PATH>`
  - `agm branch set <ENTRY> <KEY=VALUE>... [--file PATH] [--worktree PATH]` → in `Updated <ENTRY> (created|changed)` hoặc JSON `UpdateOutcome`
  - `--file` mặc định `BRANCHES.md` (thư mục hiện tại). `KEY=VALUE` tách ở dấu `=` đầu tiên; thiếu `=` → lỗi clap-style `"đối số '{arg}' phải có dạng khóa=giá-trị"`.
  - Help clap (tiếng Anh): `Branch` = "Read or update an entry in BRANCHES.md under an atomic lock".

- [ ] **Step 1: Viết test hỏng** — `branch_set_then_get_roundtrip` (`agm branch set a/x status=wip "summary=Thêm cảnh báo" --file <tmp>/BRANCHES.md` rồi `agm --json branch get a/x --file …` → `fields` chứa `["summary","Thêm cảnh báo"]`); `branch_set_rejects_missing_equals` (exit ≠ 0, stderr chứa `khóa=giá-trị`); `branch_get_missing_entry_fails`.
- [ ] **Step 2: Chạy, xác nhận hỏng** — `cargo test --offline --test cli_test branch_` → FAIL (`unrecognized subcommand 'branch'`).
- [ ] **Step 3: Thêm `Cmd::Branch` + `ops::branch_get(file: &Path, entry: &str, json: bool) -> Result<()>` và `ops::branch_set(store: &Store, file: &Path, entry: &str, pairs: &[String], worktree: Option<&str>, json: bool, now: i64) -> Result<()>`** — `branch_set` gọi `branches::update` với `hc_terminal().as_deref()`.
- [ ] **Step 4: Chạy lại** — PASS; `cargo test --offline` toàn bộ vẫn xanh.
- [ ] **Step 5: Commit** — `git commit -m "feat(cli): agm branch get/set"`

---

### Task 4: `mcp` — khung giao thức JSON-RPC và `agm mcp`

**Files:**
- Create: `src/mcp.rs`
- Modify: `src/lib.rs`, `src/main.rs` (`Cmd::Mcp`, help: "Serve agm tools to MCP clients over stdio (JSON-RPC)")
- Test: `tests/mcp_test.rs`, `tests/cli_test.rs`

**Interfaces:**
- Consumes: `Store`, `hc::Hc`, `git::Git`.
- Produces:
  - `pub const PROTOCOL_VERSIONS: [&str; 3] = ["2025-06-18", "2025-03-26", "2024-11-05"];`
  - `pub struct McpCtx<'a> { pub store: &'a Store, pub hc: &'a dyn Hc, pub git: &'a dyn Git, pub cfg_path: PathBuf, pub caller_terminal: Option<String>, pub clock: fn() -> i64 }`
  - `pub fn handle_message(ctx: &McpCtx, msg: &serde_json::Value) -> Option<serde_json::Value>`
  - `pub fn serve(ctx: &McpCtx, input: impl BufRead, output: impl Write) -> anyhow::Result<()>`
  - `pub fn tool_definitions() -> Vec<serde_json::Value>` (Task 4 trả mảng rỗng; Task 5–6 thêm)
  - `fn call_tool(ctx: &McpCtx, name: &str, args: &Value) -> Result<Value, ToolError>` với `enum ToolError { Unknown, Failed(anyhow::Error) }` (riêng tư; Task 5–6 thêm nhánh)

Hành vi đã chốt:
- `serve`: đọc từng dòng; dòng trống bỏ qua; JSON hỏng → `{"jsonrpc":"2.0","id":null,"error":{"code":-32700,…}}`; mảng (batch) → `-32600`; mỗi phản hồi là một dòng + `flush`. EOF → `Ok(())`.
- `initialize`: `protocolVersion` = bản client yêu cầu nếu thuộc `PROTOCOL_VERSIONS`, ngược lại `PROTOCOL_VERSIONS[0]`; `capabilities: {"tools": {"listChanged": false}}`; `serverInfo: {"name":"agm","version": env!("CARGO_PKG_VERSION")}`.
- Thông điệp không có `id` (notification, vd `notifications/initialized`) → `None`.
- `ping` → `{}`. `tools/list` → `{"tools": tool_definitions()}`. Method lạ → `-32601`.
- `tools/call`: tên lạ → lỗi JSON-RPC `-32602` `"unknown tool: {name}"`. Tool chạy lỗi → kết quả `{"content":[{"type":"text","text":"{err:#}"}],"isError":true}`. Thành công → `{"content":[{"type":"text","text": <JSON đẹp>}],"structuredContent": <object>,"isError":false}`.
- `main`: `Cmd::Mcp` mở `Store`, dựng `McpCtx` với `HcCli::default()`, `GitCli`, `caller_terminal = ops::hc_terminal()`, rồi `serve(stdin.lock(), stdout.lock())`.

- [ ] **Step 1: Viết test hỏng** (`tests/mcp_test.rs`, ctx dựng từ `FakeHc` + `Store::open_in_memory`)

```rust
#[test] fn initialize_echoes_supported_version_else_latest() { /* "2025-03-26" → "2025-03-26"; "1999-01-01" → "2025-06-18"; serverInfo.name == "agm" */ }
#[test] fn notification_gets_no_response() { assert!(handle_message(&ctx, &json!({"jsonrpc":"2.0","method":"notifications/initialized"})).is_none()); }
#[test] fn unknown_method_and_unknown_tool_codes() { /* "foo/bar" → -32601; tools/call name "agm_kill" → -32602 */ }
#[test] fn serve_survives_garbage_and_every_output_line_is_json() {
    // input: "not json\n[]\n{…ping id 7…}\n" → 3 dòng ra; dòng 1 code -32700, dòng 2 -32600, dòng 3 id 7 result {}
}
#[test] fn mcp_source_never_builds_user_caller() { assert!(!include_str!("../src/mcp.rs").contains("Caller::User")); }
```

`tests/cli_test.rs`: `agm_mcp_answers_initialize_over_stdio` — spawn `agm mcp` với stdin piped `initialize` + `tools/list`, đóng stdin, đọc stdout: đúng 2 dòng, mỗi dòng parse được JSON.
- [ ] **Step 2: Chạy, xác nhận hỏng** — `cargo test --offline --test mcp_test` → lỗi biên dịch.
- [ ] **Step 3: Cài đặt `src/mcp.rs` và `Cmd::Mcp`** theo "Hành vi đã chốt".
- [ ] **Step 4: Chạy lại** — `cargo test --offline --test mcp_test --test cli_test` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): stdio JSON-RPC server skeleton"`

---

### Task 5: Tool chỉ đọc — `agm_ps`, `agm_inbox`, `agm_quota`, `agm_branch_get`

**Files:**
- Modify: `src/mcp.rs`
- Test: `tests/mcp_test.rs`

**Interfaces:**
- Consumes: `Store::terminals(live_only)`, `Store::inbox(open_only)`, `report::quota_with_config`, `policy::task_tokens`, `Config::load`, `branches::get`.
- Produces (tên tool → input schema → `structuredContent`):
  - `agm_ps` → `{all?: boolean}` → `{"terminals": [TerminalRow…]}` (`all=false` ⇒ chỉ live)
  - `agm_inbox` → `{all?: boolean}` → `{"items": [InboxRow…]}` (`all=false` ⇒ chỉ mục mở)
  - `agm_quota` → `{task?: string}` → `{"quota": [QuotaRow…], "task": {"id", "tokens"} | null}`; đọc DB như hiện có (không quét log, không gọi `--refresh`); `Config::load(&ctx.cfg_path)` lỗi → tool lỗi `isError`
  - `agm_branch_get` → `{branches_file: string, entry: string}` (bắt buộc cả hai) → `{"entry", "found": bool, "fields": [[k,v]…]}`
- Mọi schema có `"additionalProperties": false`; tham số sai kiểu → tool lỗi `isError` với thông báo nêu tên tham số.

- [ ] **Step 1: Viết test hỏng** — `ps_lists_live_terminals_only_by_default`, `inbox_returns_open_items`, `quota_includes_task_tokens_when_task_given`, `branch_get_reports_found_false_for_missing_entry`, `wrong_argument_type_is_tool_error` (`agm_ps` với `{"all":"yes"}` → `isError: true`, text chứa `all`), `tools_list_has_schema_for_each_tool` (mỗi tool có `name`, `description`, `inputSchema.type == "object"`).
- [ ] **Step 2: Chạy, xác nhận hỏng** — `cargo test --offline --test mcp_test` → FAIL (tool lạ → -32602).
- [ ] **Step 3: Thêm 4 tool vào `tool_definitions` và `call_tool`.**
- [ ] **Step 4: Chạy lại** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): read-only tools ps, inbox, quota, branch_get"`

---

### Task 6: Tool ghi — `agm_send`, `agm_spawn`, `agm_branch_update` + bảo vệ

**Files:**
- Modify: `src/mcp.rs`
- Test: `tests/mcp_test.rs`

**Interfaces:**
- Consumes: `actions::{send, spawn, SpawnReq, KINDS, kind_from_english}`, `branches::{update, BranchUpdate, LOCK_TIMEOUT}`, `Config::load`.
- Produces:
  - `agm_send` → `{to: string, kind: enum[ASSIGN,REPORT,ERROR,QUESTION,BLOCKED,PASS,FAIL], task?: string, text: string}` → `MessageRow`. `kind` nhận thêm mã Việt trong `KINDS` (schema ghi enum tiếng Anh; code chấp nhận cả hai). `from = ctx.caller_terminal` hoặc `"agent"`.
  - `agm_spawn` → `{template, task, role?="worker", name?, cwd?, text?: string}` → `{"address": "<t>"}`; `text` có thì `SpawnReq.text = vec![text]`. Giữ nguyên `spawn_precheck` (max_workers, ngân sách task).
  - `agm_branch_update` → `{branches_file: string, entry: string, fields: object<string,string>, worktree?: string}` → `UpdateOutcome`; gọi `branches::update` với `ctx.caller_terminal`.
- Không thêm tool nào khác. Danh sách cuối cùng của `tools/list` (đúng thứ tự): `agm_send, agm_inbox, agm_ps, agm_quota, agm_spawn, agm_branch_get, agm_branch_update`.

- [ ] **Step 1: Viết test hỏng**

```rust
#[test] fn tool_list_is_exactly_the_phase1_set() { /* so sánh Vec tên với danh sách ở trên; không có "kill", "interrupt", "manage", "cleanup", "reply", "done" */ }
#[test] fn send_without_terminal_id_is_from_agent_never_user() {
    // ctx.caller_terminal = None; send to "user" kind "REPORT" → MessageRow.from == "agent", kind == "BÁO CÁO";
    // mục inbox mới có terminal == Some("agent")
}
#[test] fn send_accepts_vietnamese_alias_and_rejects_unknown_kind() { /* "HỎI" ok; "PING" → isError */ }
#[test] fn spawn_respects_max_workers() { /* cfg max_workers=1 (providers.toml tạm), 1 terminal role=worker live → isError chứa "max_workers"; FakeHc.calls không có "run" */ }
#[test] fn branch_update_refuses_worker_caller() { /* terminal t9 role=worker, ctx.caller_terminal=Some("t9") → isError chứa "thợ" */ }
#[test] fn no_tool_call_ever_kills() { /* gọi lần lượt mọi tool với input hợp lệ tối thiểu; FakeHc.calls không chứa "kill"/"interrupt" */ }
```

- [ ] **Step 2: Chạy, xác nhận hỏng** — `cargo test --offline --test mcp_test` → FAIL.
- [ ] **Step 3: Thêm 3 tool ghi vào `tool_definitions` và `call_tool`.**
- [ ] **Step 4: Chạy lại** — `cargo test --offline` toàn bộ + `cargo clippy --all-targets -- -D warnings` → xanh.
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): send, spawn, branch_update tools with caller guards"`

---

### Task 7: Tài liệu, phiên bản 0.4.0, kiểm chứng thật

**Files:**
- Modify: `Cargo.toml` (`version = "0.4.0"`), `Cargo.lock`, `CHANGELOG.md`, `README.md`, `README_vi.md`

- [ ] **Step 1: Cập nhật tài liệu** — README/README_vi: mục mới "MCP server" (lệnh đăng ký `claude mcp add -s user agm -- agm mcp`; khối `opencode.json` `{"mcp":{"agm":{"type":"local","command":["agm","mcp"]}}}`; bảng 7 tool; câu "agm mcp không có kill/interrupt/manage/cleanup") và dòng `agm branch get|set` trong bảng lệnh; bỏ phần mô tả khóa `mkdir` tay, thay bằng `agm branch set`. CHANGELOG mục `0.4.0`. Đổi dòng "Phiên bản hiện tại" thành 0.4.0.
- [ ] **Step 2: Kiểm chứng tự động** — `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test --offline` → tất cả xanh, ghi số test pass.
- [ ] **Step 3: Kiểm chứng thật** — `cargo install --path . --offline`; `claude mcp add -s user agm -- agm mcp`; trong một phiên Claude Code chạy dưới `hc`, gọi `agm_ps` và `agm_send` tới `user` → `agm inbox` hiện tin với `from` là địa chỉ terminal. Lặp lại với OpenCode (khối `opencode.json` ở Step 1, gọi `agm_ps`). Ghi kết quả cả hai client vào báo cáo.
- [ ] **Step 4: Commit** — `git commit -m "chore: release 0.4.0 (agm mcp, agm branch)"`
