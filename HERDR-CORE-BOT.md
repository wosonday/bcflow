# Điều phối nền với herdr-core và `agm` (`agmag`), quy tắc tin nhắn và Understand-Anything (`.ua/`)

Tài liệu hạ tầng bổ sung cho [AGENTS-TUTORIAL.md](AGENTS-TUTORIAL.md) (v4.0) và [BOT-CEO.md](BOT-CEO.md). Phạm vi: (1) dùng **`agm` (`agmag` v0.4.0)** đứng trên `hc` (`herdr-core`) để chạy nền, giao việc, đo token/quota, xoay vòng, ghi `BRANCHES.md` có khóa (`agm branch`) và MCP server cho quản lý/duyệt (`agm mcp`; chi tiết [docs/AGM-0.4.0-GUIDE.md](docs/AGM-0.4.0-GUIDE.md)); (2) tin nhắn ngắn & hộp thư; (3) thiết lập **Understand-Anything (`.ua/`)**, kiểm soát quyền ghi Shared Memory và **Ponytail** trên từng template.

---

## Phần 1. Kiến trúc `agm` + `hc` và tự động hóa nền

### 1.1. Phân chia trách nhiệm: `hc` (tay chân) vs `agm` (sổ sách & luật)

| Thành phần | Đảm nhận | Lệnh chính |
|---|---|---|
| **`hc` (`herdr-core`)** | Chạy nền terminal, đọc màn hình, gõ phím, phát `hc events --json`, đặt `HC_TERMINAL_ID` | `hc run`, `hc read`, `hc attach`, `hc ls`, `hc server start\|status` |
| **`agm` (`agmag` v0.4.0)** | Theo dõi `hc events`, đo token & hạn mức (Claude, Codex, `agy`, **OpenCode Go** `5h`/`7d`/`mo`), hộp thư, `agm send`, `max_workers`, ngân sách, xoay vòng phiên, khóa ghi `BRANCHES.md`, MCP stdio 7 tool & bảo vệ không tự kill Claude/Codex | `agm daemon`, `agm tui`, `agm spawn`, `agm send`, `agm inbox`, `agm reply`, `agm quota --refresh`, `agm manage`, `agm rotate`, `agm cleanup`, `agm budget`, `agm policy`, `agm hook`, `agm branch get\|set`, `agm mcp` |
| **Wrapper worktree (tùy chọn)** | Tạo `git worktree`, đọc `tier:` / `skills:` dạng CSV trong `.agents/tasks/<ID>.md`, gọi `agm spawn`; chọn skill theo giai đoạn, không chặn cứng ở 2 | `dispatch.sh`, `cleanup-wt.sh` |

**Lưu ý đo lường:** `agm cost --by model` tách tự động OpenCode theo `provider/id` (`opencode-go/deepseek-v4-pro`, `openai/gpt-6.1-sol`, `nvidia-kimi/moonshotai/kimi-k3.1-pro`). Cột **USD** là giá trị quy đổi theo giá API niêm yết (`assets/prices.toml`); chi phí API thực trả nằm ở `billed (API)` của `agm cost --summary`. Cài `agm` bằng `cargo install --path .` (dữ liệu mặc định tại `~/.agmag/`, đổi qua `AGM_HOME`).

### 1.2. Khởi chạy `agm daemon` & TUI

```powershell
hc server start
hc run --bg --no-parent --name agm-daemon agm daemon
agm tui                     # xem quota, terminals, inbox; gõ `view` chọn tối đa 3 bảng
```

- **Chạy khi khởi động máy:** Windows dùng Task Scheduler (*At log on*) chạy 2 lệnh đầu; Linux dùng `systemd --user`.
- **Nút dừng khẩn cấp:** `agm policy off` tắt ngay mọi hành động tự động. Chỉ người dùng mới được `agm kill` phiên Claude/Codex (xác nhận `y`).

### 1.3. Cấu hình `~/.agmag/providers.toml`, `agm budget` và `.agents/bot.env`

```toml
# ~/.agmag/providers.toml (không chứa khóa API)
[providers.codex]
billing = "subscription"
[providers.claude]
billing = "subscription"
[providers.agy]
billing = "subscription"
[providers."opencode-go"]
billing = "subscription"
[providers.openrouter]
billing = "api"

[models."gpt-6.1-sol"]
provider = "codex"
context_window = 475000

[policy]
max_workers = 3                 # trần thợ (agm.role=worker) chạy đồng thời
manager = "ceo-platform"        # terminal nhận cảnh báo (codex-mgr nếu 1 luồng)
ctx_warn = 50                   # cảnh báo chung; không phải hạn mức dùng gói
ctx_rotate = 70                 # fallback chung/workers; CEO/Codex High tự yêu cầu sớm theo policy riêng
handoff_timeout_min = 10
link_timeout_min = 5
stats_months = 4
```

```powershell
agm budget set task:login-7 --soft 2000000 --hard 5000000
agm budget set provider:codex:10080 --soft 80 --hard 95
```

Xem [quy tắc kinh phí của CEO](AGENTS-TUTORIAL.md#311-điều-phối-kinh-phí-ai). File `.agents/bot.env` trong dự án chỉ giữ cấu hình worktree gọn nhẹ:

```bash
# .agents/bot.env (đưa vào git; không chứa bí mật)
PROJECT_DIR=/duong/dan/den/du-an
BASE_BRANCH=main
WT_DIR=.wt
MANAGER=ceo-platform
RUNTIME_DIR=.agents/.runtime
RUNTIME_TTL_DAYS=7
```

Thêm vào `.gitignore` của dự án:

```gitignore
.wt/
.agents/.runtime/
.ua/intermediate/
.ua/diff-overlay.json
```

### 1.4. Template cho agent (gọn, kèm Ponytail & báo cáo chuẩn hóa)

Template nằm tại `%APPDATA%\herdr-core\templates\` (Windows) hoặc `~/.config/herdr-core/templates/` (Linux/macOS; ghi đè bằng `AGM_HC_TEMPLATES`).

Trên Windows, `hc run <template>` có thể lỗi `cannot start "sh"`; `agm` tự dự phòng chạy thẳng `command:` rồi gửi thân template + nhiệm vụ vào terminal. Nếu terminal đã mở mà brief không tới được, `agm spawn` vẫn in địa chỉ nhưng **thoát mã 3** (brief nằm trong `agm inbox`, mục `brief_failed`): không coi là thành công, không dọn worktree, không spawn lại. Đã kiểm 2026-11-24: `command:` có tham số (ví dụ `agy --model ...`) luôn hỏng vì thiếu `sh`, và TUI OpenCode 2.4 không có `--model`; khi cần khóa model, dùng dạng argv `hc run --bg --name ... opencode run --agent <vai> --model <id> "<brief>"` rồi tự kiểm `max_workers`/ngân sách ([bài học Windows](docs/WINDOWS-AGENT-RUNTIME.md)).

Các mẫu dưới đây dùng [Skill Routing](docs/SKILL-ROUTING.md): một workflow + một domain là mặc định; thêm skill đúng giai đoạn khi cần và luôn giữ verification. Đây là mẫu tài liệu; cập nhật chúng không tự sửa template đang cài trên máy hay cài skill cho mọi CLI.

**Ngữ cảnh CEO/Codex High:** theo [Context Rotation](docs/CONTEXT-ROTATION.md), phiên đã managed tự gọi `agm rotate <terminal-của-mình>` ở 50%, dự trữ đến 55% chỉ để bàn giao; task dài thường chọn 40% (35–45%) hoặc thấp hơn khi có dự báo cụ thể. Không đổi `ctx_rotate` chung chỉ để đạt ngưỡng theo vai; `agm 0.4.0` chưa có cấu hình ngưỡng riêng hoặc flag `manage` cho việc đó. Trước khi giao lại phải giữ task đang chạy và quyền đã duyệt trong handoff.

> **Quy tắc `agm` nhúng vào `AGENTS.md` / `CLAUDE.md`:**
> - Gửi tin ngắn (1–3 dòng, không truyền lịch sử hội thoại): `agm send <terminal> --type GIAO|BÁO CÁO|LỖI|HỎI|CHẶN|ĐẠT|CHƯA ĐẠT --task <ID> "<tóm tắt; xem tệp>"` (nhận cả mã tiếng Anh `ASSIGN|REPORT|ERROR|QUESTION|BLOCKED|PASS|FAIL`)
> - Cần người dùng quyết định: `agm send user --type HỎI --task <ID> "<câu hỏi>"`
> - Không dùng `agm kill` với Claude/Codex. Không sửa `.agents/memory/published/` hay `.ua/` trừ Claude CEO.
> - Ghi `BRANCHES.md` chỉ qua `agm branch set` / `agm_branch_update`. Chỉ gắn MCP `agm` (`command: ["agm", "mcp"]`) cho profile quản lý/duyệt, không cho `worker-*`.

**1) `worker-fast.md` / `worker-mid.md` / `worker-strong.md` (OpenCode Workers — Ponytail rút gọn):**

*(Ưu tiên `opencode-go/deepseek-v4.1-flash` cho `fast/mid`, `opencode-go/deepseek-v4-pro` cho `strong` sau qualification; `command: opencode` không tự khóa model nên cần kiểm model thực hoặc truyền `--model` khi dùng `opencode run`.)*

```markdown
---
description: Thợ cơ khí. Làm đúng một đầu việc trong một worktree (Ponytail rút gọn).
command: opencode
ready: quiet
---
Bạn là thợ cơ khí. Làm theo thứ tự:
1. Đọc `.agents/tasks/<ID>.md` và các mục trong `.agents/memory/published/` được trỏ ở trường `refs:` (không đọc toàn bộ `.ua/knowledge-graph.json`).
2. Đọc `phase`, `work_type`, `skills:` (CSV) và `skill_refs:` trong Brief; dùng workflow/domain đã chọn, mặc định một + một. Không chặn skill cần thiết chỉ vì vượt 2; ghi lý do bổ sung, giữ verification cuối giai đoạn và yêu cầu bắt buộc của client. Thiếu skill thì báo, không tự cài. Không sinh agent khác.
3. Quy tắc Ponytail rút gọn: (a) chỉ làm đúng Brief và chỉ sửa `files_allowed`, không mở rộng/thu hẹp yêu cầu CEO đã duyệt; (b) đọc tệp mục tiêu & tái sử dụng helper sẵn có trước khi viết mới; (c) không bỏ validation, xử lý lỗi, bảo mật hay test bắt buộc; (d) tuyệt đối không sửa cấu hình lint, không chèn `eslint-disable`/`@ts-ignore`, không chạm `.agents/memory/published/`, `.ua/` hay `BRANCHES.md`.
4. Áp dụng `verification-before-completion` hoặc playbook runtime tương đương đã duyệt; chạy lint (`eslint --max-warnings 0` / `ruff` / `clippy -D warnings`) + test trong Brief, đọc kết quả thật và nêu phần chưa kiểm trước khi báo hoàn thành.
5. Ghi ngắn gọn (≤ 300 từ) vào `.agents/log/<ID>.md` (thay đổi, lệnh kiểm + số đếm, phần chưa kiểm, implementation findings nếu có), commit với tiền tố `<ID>:`, rồi báo quản lý qua `agm send` và dừng.
```

**2) `auditor.md` (Audit & ESLint/CI — ưu tiên script/CI, model miễn phí chỉ khi cần diễn giải):**

```markdown
---
description: Audit & cổng ESLint/CI. Không sửa mã nguồn.
command: opencode
ready: quiet
---
Bạn là Audit. Không sửa mã nguồn.
Ưu tiên script/CI, không gọi LLM chỉ để chạy lệnh xác định. Nếu được giao diễn giải lỗi, chỉ đọc workflow phù hợp và giữ verification trước khi báo kết quả.
Chạy kiểm trên worktree của `<ID>`:
1. Phạm vi & quyền ghi: `git diff --name-only <base>...HEAD` nằm hoàn toàn trong `files_allowed`, khớp `.agents/log/<ID>.md`, KHÔNG chạm cấu hình lint, `.agents/memory/published/`, `.ua/` hay `BRANCHES.md`.
2. Chặn lách rule: từ chối `CHƯA ĐẠT` ngay nếu diff thêm mới `eslint-disable`, `@ts-ignore`, `@ts-nocheck`.
3. Lint & Test: chạy đúng lệnh kiểm trong Brief đạt 100%.
Ghi kết quả vào `.agents/log/<ID>.md`, commit và báo qua `agm send <MANAGER> --type BÁO CÁO --task <ID> "..."`.
```

**3) `reviewer.md` (Reviewer độc lập — Ponytail `full` khi dùng Codex Reviewer):**

*(Ưu tiên `opencode-go/deepseek-v4-pro` sau qualification; đổi họ model khác nếu Pro đã viết code. Không đọc lịch sử hội thoại của thợ.)*

```markdown
---
description: Reviewer độc lập chạy sau Audit. Không sửa mã sản phẩm.
command: opencode
ready: quiet
---
Bạn là Reviewer. Không sửa mã sản phẩm. Không đọc lịch sử chat của thợ.
Workflow chính là `code-review` đã kiểm khả dụng theo `skill_refs`, thêm domain liên quan khi cần; Ponytail full bổ trợ complexity, không thay review correctness/security. `requesting-code-review` thuộc quản lý; `receiving-code-review` thuộc implementer sửa feedback.
1. Đọc `.agents/tasks/<ID>.md`, `.agents/log/<ID>.md`, `git diff <base>...HEAD`, và truy vấn `.ua/` (hiểu tác động diff) + `.agents/memory/published/` (kiểm bất biến).
2. Kiểm tập trung: correctness, security, testing, performance, complexity (Ponytail full; không chất vấn phạm vi CEO đã duyệt).
3. Ghi kết quả ngắn gọn vào `.agents/reviews/<ID>-<n>.md` (ĐẠT | CHƯA ĐẠT; mỗi Blocker ghi `file:line`, hậu quả, cách sửa tối thiểu). Nếu có phát hiện dài hạn hoặc xung đột bộ nhớ, ghi đề xuất vào `.agents/memory/inbox/<ID>-<agent>.md`.
4. Áp dụng verification, ghi base/head đầu vào (dòng `Đối tượng: HEAD <sha>` — guard `ready` đòi tệp review nhắc đúng SHA này) và phần chưa kiểm, commit tệp review trên nhánh rồi báo `agm send <MANAGER> --type ĐẠT|CHƯA ĐẠT --task <ID> "xem .agents/reviews/<ID>-<n>.md"`.
```

**4) `sparring-opus.md` (Đồng Kiến trúc sư & Reviewer cấp cao — `agy` Opus High):**

*(Mở phiên tương tác trong repo / `--conversation <id>` để giữ `cache_read_tokens`, không dùng `agm ask` one-shot.)*

```markdown
---
description: Đồng Kiến trúc sư & Reviewer cấp cao qua agy Opus High (chỉ đọc).
command: agy --model <opus-high-slug> --mode plan --sandbox
---
Bạn là Đồng Kiến trúc sư & Reviewer cấp cao (chỉ đọc). Dùng `.ua/` và `.agents/memory/published/` để phản biện thiết kế, chia `.agents/tasks/<ID>.md`, hoặc phân xử review ca khó và gắn cờ xung đột (`type: conflict`) vào `.agents/memory/inbox/`. Báo quản lý qua `agm send`.
Khi thiết kế: chọn `system-design`, thêm `architecture` khi cần ADR, Ponytail off/lite. Khi review: chọn `code-review`, domain liên quan và verification, Ponytail full. Không nạp cả hai luồng nếu chỉ được giao một vai.
```

**5) `docs-light.md` (Cập nhật docs & báo cáo cuối — ưu tiên bậc nhẹ `agy`):**

*(Dùng `gemini-3.8-flash-medium` hoặc `gemini-4-hydrogen-low` cho `.agents/reports/<ID>.md` và docs ngắn; chỉ chuyển `gemini-4-hydrogen-medium` khi sửa tài liệu kiến trúc dài.)*

```markdown
---
description: Cập nhật docs ngắn và tổng hợp báo cáo cuối sau Reviewer, trước Copilot.
command: agy --model gemini-3.8-flash-medium
---
Bạn là agent Docs & Báo cáo. Không sửa code nghiệp vụ.
Chọn `documentation` theo skill_refs đã kiểm; chỉ thêm skill artifact khi có yêu cầu định dạng cụ thể, và giữ verification khi kết thúc.
1. Đọc `.agents/tasks/<ID>.md`, `.agents/log/<ID>.md`, `.agents/reviews/<ID>-*.md` và `git diff --stat <base>...HEAD`.
2. Cập nhật docs bị ảnh hưởng (nếu có) và viết báo cáo ngắn gọn (≤ 300 từ) vào `.agents/reports/<ID>.md`.
3. Ghi 1 mục vào `.agents/log/<ID>.md`, commit (`<ID>: docs: ...`), và báo `agm send <MANAGER> --type BÁO CÁO --task <ID> "SHA mới: <sha>"`.
```

**6) `argon-advisor.md` (Cố vấn kỹ thuật & Tối ưu Harness — `agy` Gemini Argon):**

```markdown
---
description: Cố vấn kỹ thuật & tối ưu harnessing (Gemini Argon, Ponytail full khi sửa).
command: agy --model gemini-4-argon-high
---
Bạn là Cố vấn Harnessing (Gemini Argon). Đọc `.agents/reports/`, `.agents/reviews/` và `.agents/memory/inbox/` để đề xuất hoặc cập nhật tệp harness trên nhánh riêng (áp dụng Ponytail full; không sửa code nghiệp vụ, không tự nới quy tắc cứng khi chưa có người dùng duyệt).
Ponytail off/lite lúc phân tích, full lúc sửa đã rõ phạm vi. Dùng `writing-skills` khi chỉnh skill tái sử dụng hoặc `skill-creator` khi tạo skill mới; sửa câu chữ harness thông thường không cần nạp cả bộ công cụ tạo skill.
```

### 1.5. Script giao việc `.agents/bot/dispatch.sh` (đã bỏ hoàn toàn GitNexus)

*(CEO xác nhận nguồn/model và kiểm `agm quota --refresh` trước khi chạy; không dùng script tự chạy cho model tính phí chưa duyệt. Không đưa [NVIDIA nền](docs/NVIDIA-BACKGROUND.md) qua script worker này.)*

```bash
#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/../bot.env"
cd "$PROJECT_DIR"

ID="$1"; TPL="${2:-worker-mid}"; ROLE="${3:-worker}"
[[ "$ID" =~ ^[a-z0-9][a-z0-9_-]{0,26}$ ]] || { echo "ID '$ID' không hợp lệ (chỉ a-z 0-9 - _, <= 27 ký tự)" >&2; exit 4; }
TASK=".agents/tasks/$ID.md"
[ -f "$TASK" ] || { echo "thiếu $TASK" >&2; exit 1; }

if [ "$ROLE" = worker ]; then
  tier=$(sed -n 's/^tier:[[:space:]]*//p' "$TASK" | head -1)
  case "$tier" in
    nhanh)       TPL=worker-fast ;;
    vừa|vua)     TPL=worker-mid ;;
    mạnh|manh)   TPL=worker-strong ;;
  esac
  skills=$(sed -n 's/^skills:[[:space:]]*//p' "$TASK" | head -1)
  # Mặc định một workflow + một domain; không bỏ skill bắt buộc vì trần số lượng.
  # CEO kiểm skill_refs và skill_reason trước giao; wrapper chỉ chuyển tiếp CSV.
fi

WT="$WT_DIR/$ID"
CREATED_WT=0
if [ ! -d "$WT" ]; then
  # Khóa ngắn để nhiều dispatch.sh song song không đụng .git/worktrees; không lấy được khóa thì dừng.
  mkdir -p .agents/.runtime/locks
  got=0
  for _ in $(seq 30); do mkdir .agents/.runtime/locks/wt 2>/dev/null && { got=1; break; }; sleep 0.2; done
  [ "$got" -eq 1 ] || { echo "không lấy được khóa .agents/.runtime/locks/wt" >&2; exit 5; }
  trap 'rmdir .agents/.runtime/locks/wt 2>/dev/null || true' EXIT
  if [ ! -d "$WT" ]; then
    git worktree add "$WT" -b "feat/$ID" "$BASE_BRANCH"
    CREATED_WT=1
  fi
  rmdir .agents/.runtime/locks/wt 2>/dev/null || true
  trap - EXIT
fi

# agm spawn: 0 = thành công; 3 = terminal đã mở nhưng chưa nhận brief; khác = không có gì chạy.
rc=0
agm spawn "$TPL" --task "$ID" --role "$ROLE" --cwd "$PROJECT_DIR/$WT" -- \
  "Việc: $ID. Skill: ${skills:-không}. Khi xong/chặn: agm send $MANAGER --type BÁO CÁO --task $ID \"xong, xem .agents/log/$ID.md\"" || rc=$?
if [ "$rc" -eq 3 ]; then
  # Thợ đang chạy trong worktree: KHÔNG dọn, không spawn lại; gửi brief từ agm inbox (brief_failed).
  echo "spawn $ID: terminal đã mở nhưng chưa nhận brief — xem agm inbox (brief_failed)" >&2
  exit 3
elif [ "$rc" -ne 0 ]; then
  if [ "$CREATED_WT" -eq 1 ]; then
    git worktree remove --force "$WT" || true
    git branch -D "feat/$ID" || true
  fi
  exit 2
fi
```

---

## Phần 2. Tin nhắn, Hộp thư và Dọn dẹp

- **Hai tầng lưu trữ:**
  - **Bền trong Git:** `.ua/` (Knowledge Graph), `.agents/memory/published/` (tri thức dài hạn CEO duyệt), `.agents/tasks|log|reviews|reports/` (hồ sơ đầu việc chuẩn hóa).
  - **Sổ máy & Tạm:** `~/.agmag/agm.db` (tin nhắn, inbox, quota, token), `.agents/handoff/` (trong `git info/exclude`), `.agents/.runtime/` (gitignored).
- **Quy tắc `agm send`:** đúng 7 loại (`GIAO`, `BÁO CÁO`, `LỖI`, `HỎI`, `CHẶN`, `ĐẠT`, `CHƯA ĐẠT`, hoặc mã tiếng Anh `ASSIGN`, `REPORT`, `ERROR`, `QUESTION`, `BLOCKED`, `PASS`, `FAIL`), 1–3 dòng trỏ tới tệp bền, **không dán lịch sử chat**.
- **Dọn dẹp 2 lớp tách biệt:**
  1. `agm cleanup` (chỉ người dùng, xác nhận `y/N`): dọn terminal `[HANDED OFF]` cũ sau xoay vòng.
  2. `.agents/bot/cleanup-wt.sh` (`copilot-git` chạy sau merge):

```bash
#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/../bot.env"
cd "$PROJECT_DIR"

[ -d "$RUNTIME_DIR" ] && find "$RUNTIME_DIR" -type f -mtime +"${RUNTIME_TTL_DAYS:-7}" -delete
git worktree list --porcelain | awk '/^worktree /{print $2}' | grep "/$WT_DIR/" | while read -r wt; do
  br=$(git -C "$wt" rev-parse --abbrev-ref HEAD)
  git merge-base --is-ancestor "$br" "$BASE_BRANCH" || continue
  [ -z "$(git -C "$wt" status --porcelain)" ] || continue
  git worktree remove "$wt"
  git branch -d "$br"
done
git worktree prune
```

---

## Phần 3. Understand-Anything (`.ua/`), Guard Quyền ghi & Kiểm tra trước khi chạy

### 3.1. Vận hành Understand-Anything (`.ua/`)

1. **Khởi tạo 1 lần trên mỗi repo chính:** chạy `/understand` (tạo `.ua/knowledge-graph.json`), commit `.ua/` (đã loại trừ `.ua/intermediate/` và `.ua/diff-overlay.json` trong `.gitignore`; dùng `git lfs track ".ua/*.json"` nếu >10 MB).
2. **Chia sẻ đọc cho mọi agent:** Claude (`/understand-chat`), Codex (`$understand-chat`), Agy (`install.sh antigravity`), Workers (nhận `files_allowed` trong Brief hoặc query qua Understand-Anything MCP chỉ đọc — không đọc thẳng cả file JSON).
3. **Cập nhật theo batch (không dùng hook mỗi commit):** Chỉ **Claude CEO** chạy incremental `/understand` trên `main` sau các đợt merge thay đổi cấu trúc/module để tránh xung đột JSON giữa các worktree.

### 3.2. Guard kiểm tra phân quyền Shared Memory & `.ua/`

Chạy tại bước Audit / trước khi `copilot-git` merge nhánh không phải của Claude CEO:

```bash
# Trả lỗi nếu nhánh thường tự ý sửa .agents/memory/published/ hoặc .ua/
if git diff --name-only "${BASE_BRANCH:-main}...HEAD" | grep -qE '^(\.agents/memory/published/|\.ua/)'; then
  echo "CHẶN: chỉ Claude CEO được sửa .agents/memory/published/ hoặc .ua/" >&2
  exit 1
fi
```

### 3.3. Checklist kiểm tra nhanh trước khi vận hành nền

1. `agm --version` (`0.4.0`), `hc --version`, `agm quota --refresh`, `agm cost --by source` hoạt động bình thường.
2. Cài xong **Understand-Anything** và đã có `.ua/knowledge-graph.json` cho repo chính.
3. Cài **Ponytail** đúng mức (Claude CEO `lite`/`off`; Codex & Agy `full`; Workers dùng template rút gọn; không bật `ultra` mặc định).
4. Kiểm thử 1 vòng đầy đủ với ID viết thường `test-1`: `dispatch.sh test-1` → Audit (gồm guard quyền ghi) → Reviewer → `docs-light` → `copilot-git` merge đúng SHA → `cleanup-wt.sh`.
5. Chạy kiểm tự động bộ tài liệu harness (phải exit 0):
   ```powershell
   python -B scripts/lint_harness.py
   python -B -m unittest discover -s tests -v
   ```
   Xem chi tiết phạm vi kiểm tại [docs/HARNESS-LINT.md](docs/HARNESS-LINT.md).
