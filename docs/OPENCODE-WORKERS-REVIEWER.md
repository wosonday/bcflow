# OpenCode v2 Guide — Workers, Audit & Default Reviewer

OpenCode v2 (`@opencode/cli`) is the primary execution engine for worktree-isolated automated roles: **Audit (`auditor`)**, **Repo Search (`explore`)**, **Engineering Workers (`worker-fast/mid/strong`)**, and **Default Reviewer (`reviewer`)**.

## 1. Installation & Windows 11 Gotchas (verified on v2.3.2)

1. Install v2 via:
   ```powershell
   npm install -g @opencode/cli
   opencode --version                 # must be 2.x (npm package "opencode-ai" is legacy v1)
   ```
   Verify in **both PowerShell and Git Bash** so you don't accidentally invoke a legacy v1 binary in `~/.opencode/bin`.
2. Enable auto-update: set `"update": "auto"` in `~/.config/opencode/opencode.json`.
3. **Fully quit OpenCode before upgrading:** the background `opencode serve --service` locks `.exe`, causing `npm i -g` / `opencode upgrade` to hang or delete the `opencode` command.
4. **v2 stores credentials in SQLite (`~/.local/share/opencode/opencode.db`), not `auth.json`:** check authenticated providers with `opencode auth list`. `agm` opens `opencode.db` in `READ_ONLY` mode and reads only session/message tables—never `credential`/`account`.

---

## 2. Current Routing: DeepSeek Go for Interactive Work, Scripts/CI for Deterministic Work

The workspace catalog confirms the two DeepSeek Go IDs below. Catalog presence is not role qualification. This table is dispatch policy, not an installed agent configuration or a change to the machine-wide default.

<!-- harness:routing:start -->
| Role | Preferred source | Scope |
|---|---|---|
| `auditor` | `script/CI` | Lint/tests/build + diff/log checks; approved free/local LLM only for interpretation if needed; no code edits |
| `explore` | `opencode-go/deepseek-v4.1-flash` | Interactive repo search/log summaries; read-only with file:line evidence |
| `worker-fast` | `opencode-go/deepseek-v4.1-flash` | Well-specified ≤ 2–3 file tasks with lint, typecheck and tests |
| `worker-mid` | `opencode-go/deepseek-v4.1-flash` | 3–5 file tasks within proven capability; a different tier label does not make the model stronger |
| `worker-strong` | `opencode-go/deepseek-v4-pro` | Suitable work after qualification; reserve Codex for genuinely difficult engineering/risk escalation |
| `reviewer` | `opencode-go/deepseek-v4-pro` | Fresh independent session, one stage after Audit; different model family when Pro authored the code |
<!-- harness:routing:end -->

**Reviewer order (user decision 2026-11-24):** when `agy` has quota, the default review runs on `agy` `gemini-4-hydrogen-high`, hard or high-risk cases on `gemini-4-neon-high`, and Claude Opus only after Neon fails ([agy guide](AGY-GEMINI-OPUS.md#15-reviewer-routing-hydrogen-high--neon-high--claude)). The OpenCode `reviewer` row above is the fallback when `agy` quota is unavailable, and the choice for a second opinion from a different model family.

NVIDIA stays off the critical path: see [NVIDIA-BACKGROUND.md](NVIDIA-BACKGROUND.md). It is neither a worker/reviewer nor a mandatory pre-review stage. Capable preapproved free/local sources are fallbacks when Go quota is unavailable; do not silently use Personal/API `opencode/deepseek-*` outside the plan.

### Capability, Latency and Qualification

- Keep `tier: nhanh | vừa | mạnh` for template compatibility; record **interactive/background** requirements and deadlines separately. Slow does not mean weak.
- Flash: edit one file → lint + typecheck/tests → log → commit in a disposable worktree. Unreliable tool calling disqualifies it from real code tasks.
- Pro Reviewer: review a diff with known defects, returning accurate file:line evidence, severity and verification without inventing test runs. Pro worker needs a separate code-editing trial; passing review does not qualify it to code.
- Open a fresh Reviewer session with only task/diff/log/test evidence, not the worker's reasoning history. When Pro authored code, select a capable approved **different model family**; do not sacrifice independence to keep one Go model.
- After two failed fixes, the CEO reassesses the brief and capability. Fast → mid on the same Flash model is not a capability upgrade. Escalate to Pro/Codex within allocation, retaining the worktree.

### Connect and Verify Go

1. The user has enabled Go. Use `/connect` → **OpenCode Go** only if the worker environment is not connected. Keep keys out of repositories, briefs and logs.
2. On the worker environment, check sign-in via `opencode auth list` and find actual IDs via `/models` or `opencode models | Select-String '^opencode-go/'` (PowerShell). When using `opencode run`, pass `--model <provider/model>` explicitly (Go IDs use `opencode-go/<model-id>`). Changing agents does not automatically change the model in a primary session. Do not set Go globally for every role.
3. Check `agm quota --refresh` and [Go Console](https://opencode.ai/console/go) before dispatch. Paid Go models **share** the 5-hour, weekly and monthly allowances. `agm cost` tracks tokens and their worth at list API prices, not Go allowance or invoices. Keep Extra Usage off by default; only the user can approve enabling it.
4. If Go returns `429`, check Console. If the allowance is exhausted, another paid Go model is not a workaround. Use a capable preapproved source or mark the task blocked; never fall back silently to pay-as-you-go. See the [budget policy](../AGENTS-TUTORIAL.md#311-điều-phối-kinh-phí-ai) and the [official Go guide](https://opencode.ai/docs/go/) for privacy rules.
5. If Console is unavailable, request recent figures from the user; sign-in does not prove available quota. Reserve review/rework capacity. Flash and Pro share Go allowance and are not free sources.

Examples in a trial worktree **after CEO approval of source and allocation**:

```powershell
opencode run --agent worker-fast --model opencode-go/deepseek-v4.1-flash "Read the approved task in this worktree and stay within scope."
opencode run --agent reviewer --model opencode-go/deepseek-v4-pro "Read the task, diff and test evidence; write only the scoped review."
```

The `worker-fast`/`reviewer` agents must already exist with scoped permissions. These examples do not create agents, grant permissions or prove qualification.

### Shared Context, Memory & Compact Ponytail for Workers

- **Phase skills:** use [Skill Routing](SKILL-ROUTING.md): feature logic → `test-driven-development`; unresolved bug → `systematic-debugging`; review feedback → `receiving-code-review`; independent reviewer → `code-review`. Add a relevant domain skill and preserve final verification. A mechanical task can select no workflow/domain skill; project checks still run.
- **Resolve, then dispatch:** record `phase`, `work_type`, comma-separated `skills`, actual runtime `skill_refs` and any addition reason in the brief. One workflow plus one domain is the default; do not reject a justified third/fourth skill or suppress a mandatory client workflow. A skill in a Codex plugin cache is not proof it is exposed to OpenCode. Report unresolved required skills or use an explicitly approved equivalent playbook.
- **Understand-Anything (`.ua/`):** Workers do **not** read the full `.ua/knowledge-graph.json` directly into context. Receive scoped file paths in `.agents/tasks/<ID>.md` (or use the read-only Understand-Anything MCP server when configured). Never edit `.ua/` from a worker worktree.
- **Shared Memory permissions (`Write task logs, implementation findings`):** Workers write only to `.agents/log/<ID>.md` using the standardized short report format (never forward chat history; never touch `.agents/memory/published/`).
- **Compact Ponytail for Free/Lightweight Workers (no `ultra`):** Keep worker prompts short and scope-locked: (1) implement only the Task Brief and `files_allowed`, never expand or narrow CEO-approved scope; (2) read target files and reuse existing helpers first; (3) never drop validation, error handling, security checks, or required tests; (4) report honestly what was run or skipped.
- **Lock down with ESLint & CI guardrails ([ESLINT-CI-GUARDRAILS.md](ESLINT-CI-GUARDRAILS.md)):** every `worker-fast` must pass `npx eslint . --max-warnings 0 --report-unused-disable-directives` (or `ruff` / `clippy -D warnings`) with `0 errors, 0 warnings` before committing, and is strictly forbidden from inserting `eslint-disable` or `@ts-ignore`.
- **Track tokens, not invoices:** use `agm cost --by source` for workload distribution and `agm quota --refresh` / Go Console for Go allowance. Use scripts/CI before calling a model for deterministic lint and test tasks.

---

## 3. Permissions & Lifecycle in `.opencode/agent/`

*(Standalone sessions need `mode: primary` or `all`; `mode: subagent` is only for OpenCode child sessions.)*

- **Strict per-role permissions & `agm mcp` scoping:**
  - `worker-*`: use v2 `permissions` to restrict `edit` and `shell`. Allow only approved writes; block out-of-role Git operations, `.agents/memory/published/`, `.ua/`, and deny access to secrets through both file and shell tools. **Do NOT attach `agm` MCP (`"mcp": { "agm": ... }`) to regular worker profiles** (because `agm_spawn` can spawn terminals); workers report via CLI `agm send` (`REPORT|BLOCKED|ERROR` or Vietnamese equivalents) and are rejected from writing `BRANCHES.md`.
  - `auditor` / `reviewer`: forbidden from editing product source code. Start every read-only profile with `"*": deny` and explicit `read`/`glob`/`grep` allows: denying only `bash` and `execute` exposes plugin tools such as `browse_*` and the worktree tools ([verified lessons](WINDOWS-AGENT-RUNTIME.md#3-read-only-boundary-for-opencode-agents)). Only manager/review profiles (such as `reviewer`) may mount `agm` MCP (`type: "local", command: ["agm", "mcp"]`).
- **Automatic rotation via `agm daemon`:**
  - Ordinary OpenCode workers retain the shared **`ctx_rotate` (example/default 70%)** fallback: `agm` requests a handoff in `.agents/log/<ID>.md` + commit, verifies a clean worktree, then replaces the old OpenCode session in the same worktree. A task explicitly using the **Codex High** profile follows the earlier [50% proactive request policy](CONTEXT-ROTATION.md), normally 40% for context-heavy work, by calling `agm rotate` on its own OpenCode terminal and using the existing worker handoff protocol. This does not authorize killing interactive Codex/Claude sessions or change all workers' thresholds.
- **Concurrency:** hard-capped by `[policy].max_workers` in `~/.agmag/providers.toml`.
- **NVIDIA background:** one task across all aliases is a manual CEO policy, not something `max_workers` enforces across every role.

## 4. Lint This Harness Collection

Run from the `agent-tutorial` root; no external packages required:

```powershell
python -B scripts/lint_harness.py
python -B -m unittest discover -s tests -v
```

See [HARNESS-LINT.md](HARNESS-LINT.md) for coverage and limitations. Lint checks structure, IDs and routing tables, not API qualification or prose semantics.
