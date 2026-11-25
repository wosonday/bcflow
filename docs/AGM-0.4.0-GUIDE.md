# agm 0.4.0: chức năng mới, cách tiếp cận và hướng dẫn sử dụng

> Áp dụng cho `agmag` **0.4.0** (merge vào `main` ngày 2026-11-22, commit `577ea3b`).
> Kế hoạch triển khai gốc: [`docs/superpowers/plans/2026-11-22-agm-mcp-branch-phase1.md`](superpowers/plans/2026-11-22-agm-mcp-branch-phase1.md).
> Đặc tả đề xuất: `D:\.harnessing\agent-tutorial\AGM-UPGRADE-SPEC.md`. Đây là Phase 1 của spec; Phase 2 (workflow) và Phase 3 (TUI workflow, chặn cứng ngân sách provider) chưa làm.

---

## Mục lục

1. [Tổng quan: 0.4.0 thêm gì và để làm gì](#1-tổng-quan)
2. [Cách tiếp cận và các quyết định thiết kế](#2-cách-tiếp-cận-và-các-quyết-định-thiết-kế)
3. [Cài đặt hoặc nâng cấp](#3-cài-đặt-hoặc-nâng-cấp)
4. [`agm branch`: sổ BRANCHES.md có khóa](#4-agm-branch-sổ-branchesmd-có-khóa)
5. [`agm mcp`: MCP server cho agent](#5-agm-mcp-mcp-server-cho-agent)
6. [Quy trình mẫu cho BOT-CEO, Copilot và thợ](#6-quy-trình-mẫu)
7. [Các sửa lỗi và gia cố đi kèm](#7-các-sửa-lỗi-và-gia-cố-đi-kèm)
8. [Xử lý sự cố](#8-xử-lý-sự-cố)
9. [Giới hạn đã biết và bước tiếp theo](#9-giới-hạn-đã-biết-và-bước-tiếp-theo)

---

## 1. Tổng quan

0.4.0 giải quyết hai điểm nghẽn mà spec nêu ra:

| Vấn đề trước 0.4.0 | Giải pháp ở 0.4.0 |
|---|---|
| Agent gọi `agm send` / `agm spawn` qua PowerShell hay lỗi cú pháp (cờ `--type`, ngoặc kép lồng nhau, mã tiếng Việt có dấu cách như `"BÁO CÁO"`). | **`agm mcp`**: agent gọi tool có JSON Schema (`agm_send`, `agm_spawn`, …), không phải gõ dòng lệnh. |
| BOT-CEO tự `mkdir .agents/.runtime/locks/branches` để khóa `BRANCHES.md`. Nếu phiên chết giữa chừng, khóa bị kẹt và phải chờ 10 phút rồi xóa tay. Ghi tệp bằng Edit tool có thể hỏng nếu hai bên ghi cùng lúc. | **`agm branch get/set`** và tool **`agm_branch_update`**: khóa nguyên tử, tự thu hồi sau 60 giây, ghi tệp tạm rồi đổi tên đè, kiểm tra hợp lệ trước khi ghi. |

Tóm tắt các chức năng mới:

- **`agm branch get <ENTRY>`**: đọc một mục `### <lane>/<slug>` trong `BRANCHES.md`.
- **`agm branch set <ENTRY> key=value ...`**: sửa hoặc tạo mục dưới khóa, có kiểm tra `status: ready`.
- **`agm mcp`**: MCP server qua stdio với 7 tool: `agm_send`, `agm_inbox`, `agm_ps`, `agm_quota`, `agm_spawn`, `agm_branch_get`, `agm_branch_update`.
- **Không có** tool nào để kill, interrupt, manage, cleanup, reply hay done qua MCP. Những việc đó vẫn **chỉ người dùng** làm được, bằng CLI.

Mọi lệnh của 0.3.3 giữ nguyên hành vi và cờ, trừ một chỗ: `--role` giờ được chuẩn hóa (xem §7).

---

## 2. Cách tiếp cận và các quyết định thiết kế

### 2.1. Không thêm phụ thuộc, không async

- MCP server chạy **đồng bộ** trên `std::io`: đọc từng dòng stdin, trả từng dòng stdout. Không cần `tokio` và không thêm crate nào.
- Mỗi client (Claude Code, OpenCode) tự khởi chạy một tiến trình `agm mcp` riêng. Tiến trình này mở `~/.agmag/agm.db` (SQLite WAL) như mọi lệnh `agm` khác, nên chạy song song được với `agm daemon` và CLI.

### 2.2. MCP là "agent", không bao giờ là "user"

agm dựa vào việc phân biệt **người dùng** và **agent** để bảo vệ phiên Claude/Codex: chỉ người dùng mới được kill hay interrupt chúng. MCP giữ nguyên ranh giới này bằng cấu trúc, không chỉ bằng quy ước:

1. **Danh sách tool cố định đúng 7 cái.** Không tool nào bọc `kill`, `interrupt`, `manage`, `cleanup`, `reply` hay `done`. Có test bảo đảm danh sách không bị nới.
2. **Mã nguồn `src/mcp.rs` không bao giờ dựng `Caller::User`.** Một test đọc chính tệp nguồn để kiểm điều này.
3. **Danh tính người gọi đi qua đúng một hàm, `mcp_actor()`:**
   - Lấy `HC_TERMINAL_ID` của tiến trình (terminal herdr đang chạy client), đã bỏ khoảng trắng hai đầu.
   - Nếu biến này **thiếu, rỗng hoặc bằng `user`** (không phân biệt hoa thường) thì danh tính là **`agent`**.
   - Danh tính này dùng làm người gửi của `agm_send` và làm tên người thực hiện trong nhật ký của `agm_branch_update`.
   - Vì vậy không có cách nào khiến MCP gửi tin hoặc ghi nhật ký dưới tên `user`, kể cả khi ai đó đặt `HC_TERMINAL_ID=user`.
4. **Tham số lạ bị từ chối.** Mọi schema khai báo `additionalProperties: false` và server kiểm thật. Ví dụ gửi kèm `"from": "user"` sẽ bị trả lỗi, không bị lặng lẽ bỏ qua.

> MCP **không phải sandbox**. Agent có quyền shell vẫn gõ được `agm`/`hc` trực tiếp. MCP chỉ bảo đảm rằng **bản thân nó** không bao giờ hành động thay người dùng.

### 2.3. Khóa BRANCHES.md tương thích với giao thức `mkdir` cũ

- Thư mục khóa là **`<thư mục chứa BRANCHES.md>/.agents/.runtime/locks/branches`**, đúng đường dẫn mà giao thức `mkdir` tay trước đây dùng. Nhờ vậy agm và script cũ vẫn chặn được lẫn nhau trong giai đoạn chuyển đổi.
- Lấy khóa bằng `create_dir`, thao tác nguyên tử trên cả Windows và Linux. Trong thư mục khóa có tệp `owner` chứa **mã chủ** riêng của lần lấy khóa đó.
- **Thu hồi khóa chết:** khóa cũ hơn **60 giây** được coi là của tiến trình đã chết.
  - Cách thu hồi: đổi tên thư mục khóa sang `branches.stale-*` (chỉ một tiến trình đổi tên thành công), **kiểm lại tuổi** rồi mới xóa.
  - Nếu ngay lúc đó có tiến trình khác vừa tạo khóa mới, agm trả khóa về chỗ cũ.
- **Nhả khóa:** chỉ xóa thư mục khóa nếu `owner` vẫn còn mã chủ của mình, nên không bao giờ xóa nhầm khóa của tiến trình khác.
- **Chờ khóa tối đa 30 giây.** Hết thời gian thì báo lỗi kèm lý do lần thử cuối.
- **Lỗi tạm thời trên Windows:** nếu tạo thư mục khóa bị báo "Access is denied" (thư mục cũ đang chờ xóa), agm coi như khóa đang bận và chờ tiếp.

### 2.4. Ghi tệp an toàn

Mọi lần sửa `BRANCHES.md` đều đi theo thứ tự sau:

1. **Ngoài khóa:** kiểm tên mục và giá trị trường, kiểm quyền ghi (thợ bị từ chối), kiểm tệp có tồn tại. Nếu cần kiểm `ready` thì chạy luôn `git status` và `git rev-parse` ở bước này.
2. **Lấy khóa.**
3. **Đọc tệp → sửa đúng khối của mục → kiểm `ready`.** Bước kiểm `ready` chỉ so sánh chuỗi với kết quả git đã lấy ở bước 1.
4. **Ghi ra `BRANCHES.md.tmp-<pid>` rồi đổi tên đè.**
   - Nếu Windows báo tệp đang bị giữ (mã 5, 32, 33, thường do Defender hoặc indexer), agm thử lại tối đa 10 lần, mỗi lần cách 50 ms.
   - Nếu vẫn lỗi, tệp tạm luôn bị dọn.
5. **Nhả khóa → ghi nhật ký** (`actions`, loại `branch`) cho cả trường hợp thành công lẫn thất bại.

Phần không bị sửa được giữ nguyên từng byte: kiểu xuống dòng (CRLF/LF), BOM UTF-8, các mục khác và các dòng chú thích.

### 2.5. Fail-closed và không tự đoán

- **`BRANCHES.md` phải có sẵn.** Nếu chưa có, agm báo lỗi chứ không tự tạo. Lý do: nếu agent truyền nhầm đường dẫn, một tệp mới ở chỗ sai sẽ "nuốt" cập nhật mà không ai hay.
- **Qua MCP, `branches_file` bắt buộc là đường dẫn tuyệt đối.** Thư mục làm việc của tiến trình `agm mcp` là nơi client được mở, thường là worktree của thợ, không phải checkout chính.
- **Muốn đặt `ready` mà thiếu `--worktree` thì bị từ chối,** không bỏ qua bước kiểm.

---

## 3. Cài đặt hoặc nâng cấp

> Windows không cho ghi đè `agm.exe` khi nó đang chạy, nên phải dừng daemon trước.

```powershell
# 1. Dừng daemon cũ
hc kill agm-daemon
hc rm agm-daemon
Get-Process agm -ErrorAction SilentlyContinue   # phải không in gì; nếu còn: Stop-Process -Id <pid>

# 2. Cài bản mới
cd D:\Project\agmag
cargo install --path . --offline
agm --version                                    # agm 0.4.0

# 3. Chạy lại daemon
hc run --bg --no-parent --name agm-daemon agm daemon
```

### 3.1. Đăng ký MCP cho Claude Code

```powershell
claude mcp add -s user agm -- agm mcp
claude mcp list          # phải thấy dòng agm
```

Các phiên Claude đang mở phải khởi động lại mới thấy các tool `agm_*`.

### 3.2. Đăng ký MCP cho OpenCode

Thêm vào `opencode.json` **của profile quản lý hoặc review** (BOT-CEO, Copilot, reviewer). **Không** thêm vào profile thợ thường:

```json
{
  "mcp": {
    "agm": { "type": "local", "command": ["agm", "mcp"] }
  }
}
```

Lý do giới hạn theo profile: `agm_spawn` sinh được terminal giống hệt `agm spawn` ở CLI. Thợ thường không nên có quyền đó.

### 3.3. Kiểm tra nhanh sau khi cài

Kiểm tra giao thức trực tiếp, không cần client:

```powershell
@'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"agm_ps","arguments":{}}}
'@ | agm mcp
```

Kết quả đúng là ba dòng JSON:
- dòng 1 có `"serverInfo":{"name":"agm","version":"0.4.0"}`;
- dòng 2 liệt kê 7 tool;
- dòng 3 có `"isError":false` và danh sách terminal.

Kiểm tra với client thật:
1. Trong một phiên Claude **chạy dưới herdr**, bảo nó gọi `agm_send` tới `user` với `kind: REPORT`.
2. Chạy `agm inbox`: mục mới phải ghi người gửi là **địa chỉ terminal** (ví dụ `t5`), không phải `user`.

---

## 4. `agm branch`: sổ BRANCHES.md có khóa

### 4.1. Định dạng một mục

```markdown
### tap-hoa/low-stock
- status: ready
- lane: ceo-tap-hoa
- tier: vừa
- head: 9f8e7d6c5b4a
- base: 1a2b3c4d5e6f
- depends_on: -
- migration: no
- touches_hotspot: -
- summary: Cảnh báo hàng sắp hết khi tồn dưới ngưỡng cấu hình
- verify: npm test — 42 passed
- review: opencode/non-openai, 0 bắt buộc sửa, .agents/reviews/tap-hoa--low-stock-1.md
- docs: không đổi
- registered: 2026-11-22T08:40+07:00
- git: -
```

| Thành phần | Quy tắc |
|---|---|
| Tên mục (`ENTRY`) | `<lane>/<slug>`, đúng **một** dấu `/`. Mỗi phần dài 1–64 ký tự, chỉ gồm `a-z 0-9 . _ -` (chữ thường). |
| Tiêu đề | Dòng `### <lane>/<slug>`. Được phép có chú thích phía sau, ví dụ `### a/x   # ghi chú`. |
| Khối | Kéo dài từ tiêu đề tới dòng tiêu đề Markdown kế tiếp (`#`, `##`, `###`, …) hoặc hết tệp. |
| Dòng trường | `- khóa: giá trị`. Khi **đọc**, phần sau ` # ` (khoảng trắng, `#`, khoảng trắng) được coi là chú thích và bị bỏ. |
| Khóa hợp lệ | `status lane tier head base depends_on migration touches_hotspot summary verify review docs registered git` |
| Trạng thái hợp lệ | `wip blocked ready merging merged deployed needs-rebase failed` |
| Giá trị | Một dòng, không rỗng, không chứa ` # ` hoặc tab+`# `, không bắt đầu bằng `# ` và không chỉ là `#`. Lý do: những giá trị đó khi đọc lại sẽ bị cắt mất. `#12` vẫn hợp lệ. Muốn để trống thì ghi `-`. |
| Tiêu đề trùng | Nếu một mục xuất hiện hai lần, agm báo lỗi và không ghi. |

### 4.2. Đọc một mục

```powershell
agm branch get tap-hoa/low-stock --file D:\Project\shop\BRANCHES.md
# status: ready
# lane: ceo-tap-hoa
# ...

agm --json branch get tap-hoa/low-stock --file D:\Project\shop\BRANCHES.md
# {"entry":"tap-hoa/low-stock","fields":[["status","ready"],["lane","ceo-tap-hoa"],...]}
```

Nếu mục không có, lệnh thoát với mã 1 và báo `không có mục <ENTRY> trong <PATH>`.

> Khi stdout không phải terminal (ví dụ khi pipe sang lệnh khác, hoặc agent chạy lệnh), `agm branch` mặc định in **JSON** giống như khi có `--json`. Dạng `khóa: giá trị` chỉ hiện khi bạn gõ trực tiếp trong terminal.

### 4.3. Ghi hoặc sửa một mục

```powershell
# Giữ chỗ khi bắt đầu chia việc
agm branch set tap-hoa/low-stock status=wip lane=ceo-tap-hoa tier=nhanh `
  "summary=Cảnh báo hàng sắp hết" touches_hotspot=core/models.py `
  --file D:\Project\shop\BRANCHES.md

# Báo sẵn sàng merge (bắt buộc có --worktree)
agm branch set tap-hoa/low-stock status=ready head=9f8e7d6c5b4a `
  "verify=npm test — 42 passed" "registered=2026-11-22T08:40+07:00" `
  --worktree D:\Project\shop.wt\low-stock --file D:\Project\shop\BRANCHES.md
```

- **`KEY=VALUE`:** tách ở dấu `=` **đầu tiên**, nên giá trị được phép chứa `=`. Giá trị có dấu cách thì bọc **cả cặp** trong ngoặc kép, như `"summary=..."`.
- **Mục chưa có:** được tạo mới ở cuối tệp. **Tệp** thì phải có sẵn.
- **Khóa đã có:** thay **cả dòng**, kể cả chú thích cũ.
- **Khóa mới:** chèn sau dòng trường cuối của mục.
- **Kết quả:** in `Updated <ENTRY> (created|changed)`. Với `--json`, in toàn bộ trường của mục sau khi ghi.
- **`--file`:** mặc định là `BRANCHES.md` trong thư mục hiện tại. Nên luôn truyền đường dẫn tuyệt đối tới checkout chính.

### 4.4. Bảo vệ `status: ready`

Bảo vệ được kích hoạt khi:
- cập nhật đặt `status=ready`, **hoặc**
- cập nhật đổi `head` của một mục **đã** `ready`.

Khi đó, mọi điều kiện sau phải đúng:

| Điều kiện | Nếu sai |
|---|---|
| `head` là hex chữ thường dài 12–40 ký tự (lấy từ lần cập nhật hoặc từ tệp) | Bị từ chối. Khi lần cập nhật đặt `status=ready` kèm `head` sai, agm từ chối ngay mà không gọi git. |
| Có `--worktree` (CLI) hoặc `worktree` (MCP) | Bị từ chối. |
| `git status` trong worktree sạch | Bị từ chối, vì còn thay đổi chưa commit. |
| HEAD thật của worktree **bắt đầu bằng** `head` | Bị từ chối, vì SHA không khớp. |

Khi bị từ chối, tệp **không đổi một byte**. Lần thử vẫn được ghi vào nhật ký `actions`.

### 4.5. Ai được ghi

- Terminal có `role = worker` trong `agm.db` bị từ chối, kèm lời nhắc báo BOT-CEO qua `agm send`. Thợ sinh bằng `agm spawn` mặc định có vai trò này.
- BOT-CEO hoặc Copilot mở tay (không có role), hay chạy ngoài herdr, thì được ghi.
- Qua MCP, việc kiểm dùng danh tính `mcp_actor()` (xem §2.2).

### 4.6. Chuyển đổi từ giao thức `mkdir` cũ

- Đừng tự `mkdir` hay `rmdir` thư mục `.agents/.runtime/locks/branches` nữa. agm giữ khóa chỉ trong vài mili giây, nhưng sẽ **thu hồi khóa nào cũ hơn 60 giây**. Một agent giữ khóa tay quá 60 giây có thể bị thu hồi giữa chừng, dẫn tới mất cập nhật.
- `AGENTS-TUTORIAL.md`, `BOT-CEO.md`, `HERDR-CORE-BOT.md` và `docs/*.md` trong cả `agmag` lẫn `/mnt/d/.harnessing/agent-tutorial/` đã được cập nhật và đồng bộ theo `agm 0.4.0`.

---

## 5. `agm mcp`: MCP server cho agent

### 5.1. Giao thức

- **Truyền tải:** JSON-RPC 2.0 qua stdin/stdout, **mỗi dòng một thông điệp**. Stdout chỉ chứa JSON; mọi log đi ra stderr.
- **Phiên bản giao thức:** `2025-06-18`, `2025-03-26`, `2024-11-05`. Client xin phiên bản được hỗ trợ thì agm trả đúng phiên bản đó; xin bản khác thì agm trả `2025-06-18`.
- **Method:** `initialize`, `notifications/initialized` (không trả lời), `ping`, `tools/list`, `tools/call`.
- **Mã lỗi:**

| Tình huống | Phản hồi |
|---|---|
| Dòng không phải JSON hoặc không phải UTF-8 | `-32700`, server **chạy tiếp** |
| JSON là mảng (batch) hoặc không phải object | `-32600` |
| Method lạ | `-32601` |
| Tên tool lạ (ví dụ `agm_kill`) | `-32602 unknown tool: …` |
| Tool chạy lỗi: tham số sai, bị từ chối, hệ thống lỗi | Kết quả `isError: true`, nội dung lỗi (tiếng Việt) nằm trong `content[0].text` |
| Tool thành công | `isError: false`; `structuredContent` là object JSON, `content[0].text` là bản in đẹp |

### 5.2. Bảng tool

| Tool | Tham số (bắt buộc **in đậm**) | Trả về (`structuredContent`) | Ghi chú |
|---|---|---|---|
| `agm_send` | **`to`**, **`kind`**, **`text`**, `task` | Bản ghi tin (`from`, `to`, `kind`, `status`, …) | `kind` nhận `ASSIGN REPORT ERROR QUESTION BLOCKED PASS FAIL`, và cả mã Việt (`GIAO`, `BÁO CÁO`, …). Tin được lưu bằng mã Việt như CLI. `to: "user"` thì tin vào hộp thư. Gửi tới terminal mà thất bại thì trả `isError: true` kèm chi tiết. |
| `agm_inbox` | `all` (bool) | `{"items": [...]}` | Mặc định chỉ lấy mục đang mở. |
| `agm_ps` | `all` (bool) | `{"terminals": [...]}` | Mặc định chỉ lấy terminal đang sống. |
| `agm_quota` | `task` | `{"quota": [...], "task": {"id", "tokens"} \| null}` | Đọc DB do `agm daemon` giữ mới; không quét log, không refresh. `providers.toml` hỏng thì báo lỗi. |
| `agm_spawn` | **`template`**, **`task`**, `role` (mặc định `worker`), `name`, `cwd`, `text` | `{"address": "t…"}` | Giống `agm spawn`: kiểm `max_workers` và ngân sách `task:<ID>`. `role` được chuẩn hóa. |
| `agm_branch_get` | **`branches_file`** (tuyệt đối, tên tệp `BRANCHES.md`), **`entry`** | `{"entry", "found", "fields"}` | Tệp chưa có thì trả `found: false`. |
| `agm_branch_update` | **`branches_file`** (tuyệt đối), **`entry`**, **`fields`** (object chuỗi→chuỗi), `worktree` | Toàn bộ trường của mục sau khi ghi, cùng `created` | Quy tắc như `agm branch set`; thợ bị từ chối. |

### 5.3. Ví dụ gọi tool

Agent gọi tool qua client; đây là dạng `arguments` cần truyền.

```json
// agm_send: báo cáo cho người dùng
{"to": "user", "kind": "REPORT", "task": "low-stock", "text": "Nhánh tap-hoa/low-stock ready tại 9f8e7d6c5b4a"}

// agm_send: giao việc cho Copilot
{"to": "copilot-git", "kind": "ASSIGN", "task": "low-stock", "text": "Merge tap-hoa/low-stock tại 9f8e7d6c5b4a, xem BRANCHES.md"}

// agm_spawn: sinh thợ
{"template": "worker-fast", "task": "low-stock", "cwd": "D:\\Project\\shop.wt\\low-stock", "text": "Đọc .agents/tasks/low-stock.md rồi làm"}

// agm_branch_update: báo sẵn sàng
{"branches_file": "D:\\Project\\shop\\BRANCHES.md",
 "entry": "tap-hoa/low-stock",
 "fields": {"status": "ready", "head": "9f8e7d6c5b4a", "verify": "npm test — 42 passed"},
 "worktree": "D:\\Project\\shop.wt\\low-stock"}
```

### 5.4. Những gì MCP **không** làm

| Muốn làm | Dùng cách này (chỉ người dùng) |
|---|---|
| Dừng hay ngắt một agent | `agm kill <t>` / `agm interrupt <t>` (phải gõ `y`) |
| Trả lời câu hỏi của agent | `agm reply <#> <text>` |
| Đóng mục hộp thư | `agm done <#\|a-b\|--kind K>` |
| Bật xoay vòng phiên | `agm manage <t>` |
| Dọn phiên đã bàn giao | `agm cleanup` |

Lưu ý: tin gửi qua MCP mà **không có** `HC_TERMINAL_ID` sẽ mang người gửi là `agent`. `agm reply` trên mục đó sẽ báo không có terminal để trả lời; hãy dùng `agm done <#>`.

---

## 6. Quy trình mẫu

### 6.1. BOT-CEO một lane (qua MCP)

1. **Xem tình hình:** `agm_ps`, `agm_quota` (kèm `task` nếu cần biết token đã dùng), `agm_inbox`.
2. **Đọc sổ chung:** `agm_branch_get` các mục có thể chạm cùng điểm nóng (`touches_hotspot`, `depends_on`).
3. **Giữ chỗ:** `agm_branch_update` với `fields: {"status":"wip","lane":"ceo-<lane>","touches_hotspot":"…"}`.
4. **Sinh thợ:** `agm_spawn` (template theo tier). Nếu đã chạm `max_workers` thì tool báo lỗi; chờ một thợ xong rồi gọi lại.
5. **Nhận báo cáo:** thợ gửi `agm_send` tới BOT-CEO với `kind: REPORT`. BOT-CEO đọc tin qua `agm_inbox` hoặc qua terminal của mình.
6. **Chốt nhánh:** sau khi audit, review và docs đều xong, gọi `agm_branch_update` với `status: ready`, `head: <SHA>` và `worktree`.
7. **Giao merge:** `agm_send` tới `copilot-git` với `kind: ASSIGN`.

### 6.2. Copilot (`copilot-git`)

1. Đọc các mục `ready` bằng `agm_branch_get` (hoặc `agm branch get`).
2. Đặt `status: merging` trước khi merge, sau đó `merged`/`deployed`, hoặc `needs-rebase`/`failed` kèm `git: <lý do hoặc đường dẫn log>`.
3. Báo kết quả bằng `agm_send` (`PASS`/`FAIL`) tới lane tương ứng.

### 6.3. Thợ (worker)

- Không ghi `BRANCHES.md`: agm sẽ từ chối.
- Báo cáo bằng `agm send` (CLI) hoặc `agm_send` (nếu profile có MCP):
  ```powershell
  agm send ceo-tap-hoa --type REPORT --task low-stock "Đã commit abc123, test 42/42"
  ```

---

## 7. Các sửa lỗi và gia cố đi kèm

| Thay đổi | Ảnh hưởng tới bạn |
|---|---|
| Nhiều tiến trình mở **DB mới tinh** cùng lúc không còn lỗi "database is locked": agm tự thử lại khi gặp BUSY/LOCKED lúc khởi tạo. | Mở nhiều phiên Claude có `agm mcp` ngay sau khi cài mới không còn bị lỗi. |
| `--role` / `role` được **chuẩn hóa** (bỏ khoảng trắng, chuyển chữ thường); role rỗng bị từ chối. | `Worker` hay `worker ` vẫn tính vào `max_workers` và vẫn bị chặn ghi `BRANCHES.md`. Terminal cũ có role trống được xoay vòng như `worker`. |
| `agm reply` trên mục MCP không rõ terminal báo lỗi rõ ràng. | Dùng `agm done <#>` cho những mục đó. |
| Git của bước kiểm `ready` chạy **trước** khi lấy khóa. | Git chậm không còn làm khóa bị thu hồi giữa chừng. |
| Đổi tên tệp thử lại khi gặp Windows mã 5/32/33. | Ít lỗi chập chờn hơn khi Defender quét `BRANCHES.md`. |

---

## 8. Xử lý sự cố

| Triệu chứng | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| `cargo install` báo lỗi ghi `agm.exe` | `agm daemon` (hoặc một `agm mcp`) đang chạy | Dừng daemon (§3), đóng các phiên Claude/OpenCode đang dùng MCP, rồi cài lại. |
| Claude không thấy tool `agm_*` | Chưa đăng ký, hoặc phiên mở trước khi đăng ký | `claude mcp list`, rồi mở phiên mới. |
| `agm_branch_*` báo lỗi `branches_file` | Đường dẫn tương đối, hoặc tên tệp không phải `BRANCHES.md` | Truyền đường dẫn tuyệt đối tới `BRANCHES.md` ở checkout chính. |
| Báo "không thấy … — tạo tệp BRANCHES.md trước" | Tệp chưa có (agm không tự tạo) | Tạo tệp có một dòng tiêu đề (ví dụ `# BRANCHES`) rồi gọi lại. |
| "BRANCHES.md đang bị khóa … (quá 30s)" | Một tiến trình giữ khóa lâu, thường là script `mkdir` tay chưa `rmdir` | Kiểm tra không còn ai đang ghi. agm tự thu hồi khóa cũ hơn 60 giây, nên chạy lại sau một phút. |
| Đặt `ready` bị từ chối | Thiếu `--worktree`, worktree còn thay đổi, `head` sai định dạng hoặc không khớp HEAD | Commit hết, `git rev-parse HEAD` trong worktree, rồi dùng 12+ ký tự đầu của SHA làm `head`. |
| "thợ (…) không được ghi BRANCHES.md" | Terminal gọi có role `worker` | Thợ báo BOT-CEO qua `agm send`; BOT-CEO ghi thay. |
| `agm_send` trả `isError` "gửi tới … thất bại" | Terminal đích không tồn tại hoặc không nhận input | `agm_ps` xem địa chỉ hoặc tên đúng; tin vẫn được lưu với trạng thái `failed`. |
| `agm_quota` trả số liệu cũ | `agm daemon` không chạy | Chạy lại daemon (§3), hoặc gõ `agm quota --refresh` ở CLI. |
| Thấy thư mục `.agents/.runtime/locks/branches.stale-*` | Còn sót lại sau một lần thu hồi khóa | Vô hại; xóa tay được khi không có ai đang ghi. |

---

## 9. Giới hạn đã biết và bước tiếp theo

### 9.1. Giới hạn của 0.4.0

- **MCP không phải sandbox.** Agent có shell vẫn gọi CLI được. Muốn hạn chế thật thì giới hạn **profile** nào được bật MCP (§3.2).
- **`agm_spawn` nhận lệnh thô** khi `template` không phải template thật, giống CLI `agm spawn`. Đây là lý do chỉ bật MCP cho profile quản lý.
- **Trên Linux/macOS còn một khe cỡ micro giây** khi trả khóa về chỗ cũ lúc thu hồi; Windows không bị. Đã ghi chú trong `src/branches.rs`.
- **Quy tắc giá trị một dòng:** các trường `verify`/`review` dài nên tóm tắt và trỏ tới tệp log hay tệp review.
- **Thứ tự khóa khi thêm qua MCP:** các khóa **mới** trong `fields` được chèn theo thứ tự chữ cái (vì JSON object không giữ thứ tự). Khóa đã có thì giữ nguyên vị trí.

### 9.2. Phase 2 và 3 trong spec (chưa làm)

- **Phase 2:** `agm workflow` và bảng `task_runs` (schema **v5**, vì v4 đã dùng). Máy trạng thái bền vững `queued → working → gate_checking → reviewing → docs_updating → ready`, có Deterministic Gate chạy không tốn token LLM, và tự nâng `tier` sau 2 lần fail.
  - Lưu ý an toàn khi làm: `gate_cmd` không được lấy trực tiếp từ tệp task hay từ tham số MCP, vì agent ghi được vào đó và daemon chạy với quyền người dùng. Nên chỉ chạy lệnh thuộc allowlist trong `providers.toml`.
- **Phase 3:** mục `── WORKFLOWS ──` trong `agm tui`, và `agm budget … --action block` để chặn cứng ngân sách provider.
